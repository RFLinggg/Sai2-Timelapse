"""三层渐进式 SAI2 版本识别 —— 让「换个汉化包就完全不认识」不再成立。

背景：主程序一直只按 exe 的 MD5 精确查表。国内大量的 [Saier] / PivInk 重打包版
只改菜单资源，底层结构和官方版一模一样（offset、字段表都不变），但 MD5 变了，
于是老版本直接不认识 —— 用户看到的是「画布下拉是空的」。

约定：能否读一张画布，取决于 (session_offset, 布局, 字段表) 这三样，**不**取决于
MD5。MD5 只是「我已经知道这三样」的索引。所以：

    第 1 层  MD5 精确命中       —— 零开销，在 sai_api_lookup 里查（调用方负责）
    第 2 层  已知偏移试探       —— 拿所有验证过的 (offset, 布局) 组合逐个试
    第 3 层  结构扫描兜底       —— 模块映像里的指针槽 + 结构特征，全新版本才走到这

和 sniffer/sniffer.py 的关系：底层读法（_u64 / _read_module / scan_structural …）
与嗅探器同源同语义。**故意没有合并成一份** —— 合并要改嗅探器，而嗅探器是用户
反馈报告的唯一入口，改坏了就等于把反馈渠道掐了，而它现在没有可靠的自动化回归。
等主程序稳定到能跑一轮完整的「未知版本 → 报告 → 自动适配」对比，再合并。

安全边界（这条比速度重要）：
  · 探测跑在后台线程，带 deadline，超时就当失败，绝不卡界面
  · 只有「真读出一张比例正确的画布」才算命中，读不出就当没命中
  · 命中的条目合并写进 ~/.art-timelapse-versions.json，不覆盖用户已有的其他条目
  · 探测全程只读，不写 SAI2 的内存，不改它的任何设置
"""
import ctypes
import json
import logging
import re
import struct
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np
import psutil
from PyMemoryEditor import OpenProcess

from . import sai

# ---------------------------------------------------------------- 布局常量
# 与 sniffer/sniffer.py 的 LAYOUTS 保持一致。next_canvas 在所有已知布局里都是 0x000。
LAYOUTS = {
    'base': dict(name=0x2ac, w=0x250, h=0x254),
    'new': dict(name=0x8f8, w=0x028, h=0x02c),
}

# new 布局必须连字段表一起给，光有 offset 读出来是花屏。
NEW_FIELDS = [
    [0, 'next_canvas', 'ptr'],
    [8, 'prev_canvas', 'ptr'],
    [16, 'prev_user_canvas', 'ptr'],
    [24, 'next_user_canvas', 'ptr'],
    [40, 'width', 'int32'],
    [44, 'height', 'int32'],
    [72, 'tile_maps', 'tile_maps'],
    [384, 'tile_count_x', 'uint32'],
    [388, 'tile_count_y', 'uint32'],
    [2296, 'name', 'utf16', 260],
    [2816, 'short_path', 'utf16', 260],
]

CACHE_PATH = Path.home() / '.art-timelapse-versions.json'
EXTERNAL_SCHEMA = 1

MAX_NODES = 64
PAGE = 0x1000
# 读到比这更小的图一律当「offset 指错了」。SAI 建不出这么小的画布。
MIN_SIDE = 8
# 读出图的宽高比和画布字段的容差。2% 吸收缩略图取整，又能抓住真正的错位。
RATIO_TOLERANCE = 0.02
# 验证时取「最大边不超过这个值」的那一级 tile map：够看清比例，解码又快。
VERIFY_MAX_SIDE = 400


# ---------------------------------------------------------------- exe 版本资源
def read_version_info(path: str) -> dict:
    """读 exe 的 VS_VERSIONINFO（纯 ctypes，几十毫秒）。

    缓存里那条记录的名字要用它 —— 传进来的 version_name 往往是
    「未识别版本」这种占位串，写进用户版本表以后，日志和下拉里就一直是这五个字，
    回头查「这到底是哪个构建」还得重新翻 exe。
    """
    out = {}
    try:
        ver = ctypes.WinDLL('version')
    except OSError:
        return out

    size = ver.GetFileVersionInfoSizeW(ctypes.c_wchar_p(path), None)
    if not size:
        return out
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(ctypes.c_wchar_p(path), 0, size, buf):
        return out

    ptr = ctypes.c_void_p()
    length = wintypes.UINT()
    buf_addr = ctypes.addressof(buf)

    def query(sub):
        if not ver.VerQueryValueW(buf, ctypes.c_wchar_p(sub),
                                  ctypes.byref(ptr), ctypes.byref(length)):
            return None
        if not ptr.value or not length.value:
            return None
        # 不能信 length：SAI 的版本资源把长度写成「字符数」，VerQueryValueW
        # 老实照做会把字符串截半。读到双字节 null 为止。
        off = ptr.value - buf_addr
        avail = max(0, size - off)
        raw = ctypes.string_at(ptr.value, min(avail, 512))
        return raw.decode('utf-16le', 'ignore').split('\x00')[0]

    translations = []
    if ver.VerQueryValueW(buf, ctypes.c_wchar_p('\\VarFileInfo\\Translation'),
                          ctypes.byref(ptr), ctypes.byref(length)) and ptr.value:
        count = length.value // 4
        raw = ctypes.string_at(ptr.value, length.value)
        for i in range(count):
            lang, cp = struct.unpack_from('<HH', raw, i * 4)
            translations.append(f'{lang:04x}{cp:04x}')
    if not translations:
        translations = ['040904b0', '040904e4', '041104b0']

    keys = ('FileDescription', 'ProductVersion', 'FileVersion')
    for tr_key in translations:
        got = {k: query(f'\\StringFileInfo\\{tr_key}\\{k}') for k in keys}
        if any(got.values()):
            out.update({k: v for k, v in got.items() if v})
            out['translation'] = tr_key
            break
    return out


def display_name(pid, exe_hash, fallback=''):
    """拼一个能给人看的版本名：「SAI Ver.2 (64bit) Preview.2024.05.22」。"""
    try:
        vi = read_version_info(psutil.Process(pid).exe())
    except Exception:
        vi = {}
    desc = re.sub(r'^PaintTool\s+', '', (vi.get('FileDescription') or '').strip())
    if desc:
        return desc
    prod = (vi.get('ProductVersion') or '').strip()
    if prod:
        return f'SAI Ver.2 (64bit) {prod}'
    return fallback or f'SAI Ver.2 ({exe_hash[:8]})'


# ---------------------------------------------------------------- 内存小工具
def _u64(proc, addr):
    try:
        return int.from_bytes(proc.read_bytes(addr, 8), 'little')
    except Exception:
        return 0


def _i32(proc, addr):
    try:
        return int.from_bytes(proc.read_bytes(addr, 4), 'little', signed=True)
    except Exception:
        return -1


def _valid_ptr(v):
    return 0x10000 <= v < 0x0000800000000000


def _decode_name(proc, node, name_off, limit=0x100):
    try:
        raw = proc.read_bytes(node + name_off, limit)
    except Exception:
        return ''
    text = raw.decode('utf-16le', 'ignore')
    for sep in ('\x00', '\uffff'):
        if sep in text:
            text = text.split(sep)[0]
    return text.strip()


def _walk(proc, head):
    out, seen, p = [], set(), head
    for _ in range(MAX_NODES):
        if not _valid_ptr(p) or p in seen:
            break
        seen.add(p)
        out.append(p)
        nxt = _u64(proc, p)
        if not _valid_ptr(nxt) or nxt == p:
            break
        p = nxt
    return out


def _heap_regions(proc, base, size):
    """模块映像之外的可读区。结构扫描只在这些范围里找指针。"""
    out = []
    try:
        for r in proc.get_memory_regions():
            if not r.is_readable:
                continue
            if r.address < base + size and r.address + r.size > base:
                continue
            out.append((r.address, r.address + r.size))
    except Exception:
        pass
    return out


def _read_module(proc, base, size, limit=16 * 1024 * 1024):
    """按页批量读模块映像。整块读出比逐 8 字节读快两个数量级。"""
    end = base + min(size, limit)
    chunks = []
    addr = base
    while addr < end:
        try:
            data = proc.read_bytes(addr, PAGE)
        except Exception:
            data = b''
        if len(data) < PAGE:
            data += b'\x00' * (PAGE - len(data))
        chunks.append(data)
        addr += PAGE
    return b''.join(chunks)


# ---------------------------------------------------------------- 候选池
def _version_tuple(text):
    """从版本名里抠日期：'SAI Ver.2 (64bit) Preview.2024.05.22' -> (2024, 5, 22)。"""
    m = re.search(r'(\d{4})\.(\d{1,2})(?:\.(\d{1,2}))?', text or '')
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))


def _date_distance(a, b):
    if not a or not b:
        return 9999
    # 只比到「年月」就够：同一个月的构建 offset 基本一致，差几天没有排序价值。
    return abs((a[0] * 12 + a[1]) - (b[0] * 12 + b[1]))


def layout_of(cls) -> str:
    """内置类怎么区分 base / new：看它有没有自己那份 SAICanvas 字段表。"""
    return 'new' if cls.SAICanvas is not sai.SAIv2_API_Base.SAICanvas else 'base'


def known_combos(exclude_hash=None):
    """把所有验证过的 (offset, 布局) 去重成候选池。

    只收 SAIv2 的子类 —— SAI1 是 32 位另一套结构，套到 sai2.exe 上永远读不出东西，
    而且它会让候选池凭空多两个必然失败的试探。
    """
    combos = {}
    for exe_hash, cls in sai.sai_api_lookup.items():
        if not issubclass(cls, sai.SAIv2_API_Base):
            continue
        if exclude_hash and exe_hash == exclude_hash:
            continue
        layout = layout_of(cls)
        key = (int(cls.session_offset), layout)
        item = combos.get(key)
        if item is None:
            combos[key] = {
                'offset': int(cls.session_offset),
                'layout': layout,
                'hits': 1,
                'names': [cls.version_name],
                'date': _version_tuple(cls.version_name),
            }
        else:
            item['hits'] += 1
            item['names'].append(cls.version_name)
    return list(combos.values())


def rank_combos(combos, exe_version_text=''):
    """按「和当前 exe 的日期接近程度」排序。

    早期版本按 offset 大小或 base/new 硬优先级排，2024.01.06（0x320220）比
    2024.02.22（0x320420）更早这件事直接把这个假设推翻了 —— offset 不随时间单调。
    所以排序改成日期邻近：版本名里的日期越接近当前 exe，越先试。
    没有日期的（2019 build）排最后，但仍然会试到。
    """
    exe_date = _version_tuple(exe_version_text)
    for c in combos:
        c['distance'] = _date_distance(exe_date, c['date'])
    return sorted(combos, key=lambda c: (c['distance'], -c['hits'], -c['offset']))


# ---------------------------------------------------------------- 校验
def _pick_level(api, canvas, hi=VERIFY_MAX_SIDE):
    """挑一级够小、又看得清比例的 tile map。不取最粗那级：7000x7000 的最粗只有 7x7。"""
    level = 0
    for i in range(sai.SAIv2_API_Base.map_count):
        try:
            tm = canvas.tile_maps[i].get(api.proc).get(api.proc)
        except Exception:
            break
        if tm.width > 0 and tm.height > 0:
            level = i
            if max(tm.width, tm.height) <= hi:
                break
    return level


def verify(proc, offset, layout, exe_hash, version_name, deadline=None):
    """造个类、列画布、真读一张图。这就是「这个 offset 能不能用」的唯一判据。

    读不出图不算通过 —— 错 offset 时 get_canvas_image 常常返回 0x0 的空数组，
    np.asarray 一样不报错。2026-11-18 那份报告就是因此把「1x13 的垃圾」判成成功。
    """
    result = {
        'ok': False, 'offset': offset, 'layout': layout, 'error': None,
        'canvas_count': 0, 'head_name': '', 'canvases': [], 'level': None,
    }
    try:
        spec = {'hash': exe_hash or '0' * 32, 'name': version_name,
                'offset': hex(offset), 'layout': layout}
        if layout == 'new':
            spec['fields'] = NEW_FIELDS
        cls = sai.make_external_version_class(spec)
        api = cls(proc)

        # 头节点单独看一次：get_canvas_list() 已经 [1:] 丢掉了占位画布，
        # 而「头是便签薄/便签本」正是这个 offset 指对了的最强旁证。
        head_name = ''
        try:
            head_ptr = sai.RPOINTER(sai.RPOINTER(api.SAICanvas))(
                api.base_address + offset).get(proc)
            nodes = api.collect_canvases(head_ptr)
            if nodes:
                head_name = nodes[0].get_name()
        except Exception:
            pass
        result['head_name'] = head_name

        canvases = api.get_canvas_list()
        result['canvas_count'] = len(canvases)
        if not canvases:
            result['error'] = '画布列表为空（链表上只有占位节点，或者 offset 不对）'
            return result

        for canvas in canvases[:3]:
            if deadline is not None and time.monotonic() > deadline:
                result['error'] = '探测超时'
                return result
            entry = {'name': '', 'size': None, 'read': False, 'error': None}
            try:
                entry['name'] = canvas.get_name()
            except Exception:
                entry['name'] = '?'
            try:
                entry['size'] = [int(canvas.width), int(canvas.height)]
            except Exception:
                entry['size'] = None
            if not entry['size'] or min(entry['size']) <= 0:
                entry['error'] = f'宽高非法 {entry["size"]}'
                result['canvases'].append(entry)
                continue

            entry['level'] = _pick_level(api, canvas)
            result['level'] = entry['level']
            try:
                arr = np.asarray(api.get_canvas_image(canvas, entry['level']))
                h = arr.shape[0] if arr.ndim >= 1 else 0
                w = arr.shape[1] if arr.ndim >= 2 else 0
                entry['array_shape'] = list(arr.shape)
                if w < MIN_SIDE or h < MIN_SIDE:
                    entry['error'] = f'读出空图 {w}x{h}'
                else:
                    cw, ch = entry['size']
                    declared = cw / ch
                    actual = w / h
                    if abs(actual - declared) / declared > RATIO_TOLERANCE:
                        entry['error'] = (f'宽高比不对：画布 {cw}x{ch}，'
                                          f'读出来 {w}x{h}')
                    else:
                        entry['read'] = True
                        entry['mean'] = round(float(arr.mean()), 2)
                        entry['std'] = round(float(arr.std()), 2)
            except Exception as exc:
                entry['error'] = f'{type(exc).__name__}: {exc}'
            result['canvases'].append(entry)

        result['ok'] = any(c.get('read') for c in result['canvases'])
        if not result['ok']:
            first = next((c.get('error') for c in result['canvases'] if c.get('error')), '')
            result['error'] = first or '画布读不出来（布局与 offset 不匹配）'
        return result
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        return result


# ---------------------------------------------------------------- 第 3 层：结构扫描
def _candidate(offset, node, layout, head_name, w, h):
    return {'offset': offset, 'node': node, 'layout': layout,
            'head_name': head_name, 'width': w, 'height': h, 'chain_len': 1}


def scan_offsets(proc, base, size, log=logging.info, deadline=None):
    """不依赖任何字符串：批量扫模块映像里的指针槽，用结构特征筛。

    筛条件全部满足才算候选：
      · 槽里存的值落在可读堆区
      · 节点地址页对齐（SAI 的画布节点是页分配的，这条砍掉九成噪声）
      · 节点 +宽/高 读出来是合理正整数
      · 节点 next_canvas 是 0 或合法指针
      · 节点名字能解码成可打印文本
    """
    regions = _heap_regions(proc, base, size)
    if not regions:
        log('拿不到堆区列表，跳过结构扫描')
        return []
    raw = _read_module(proc, base, size)
    vals = np.frombuffer(raw[:len(raw) // 8 * 8], dtype='<u8')
    addrs = base + np.arange(len(vals), dtype=np.uint64) * 8

    mask = np.zeros(len(vals), dtype=bool)
    for lo, hi in regions:
        mask |= (vals >= np.uint64(lo)) & (vals < np.uint64(hi))
    cand_addr = addrs[mask]
    cand_val = vals[mask]
    log(f'模块内 {len(vals)} 个槽，落在堆区的 {len(cand_val)} 个')

    found = []
    for key, lay in LAYOUTS.items():
        for addr, val in zip(cand_addr, cand_val):
            if deadline is not None and time.monotonic() > deadline:
                log('结构扫描超时，用已找到的候选继续')
                return found
            node = int(val)
            if node & (PAGE - 1):
                continue
            w, h = _i32(proc, node + lay['w']), _i32(proc, node + lay['h'])
            if not (0 < w < 0xffff and 0 < h < 0xffff):
                continue
            nxt = _u64(proc, node)
            if nxt and not _valid_ptr(nxt):
                continue
            name = _decode_name(proc, node, lay['name'])
            if not name or not all(0x20 <= ord(c) < 0xfffe for c in name):
                continue
            rel = int(addr) - base
            if _u64(proc, base + rel) != node:
                continue
            cand = _candidate(rel, node, key, name, w, h)
            cand['chain_len'] = len(_walk(proc, node))
            found.append(cand)
    return found


def _score(c):
    """链表头是「不带扩展名的占位画布」这条权重最高 —— 主程序 get_canvas_list()
    正是用 [1:] 丢掉第一个节点的，选错 offset 的表现是「下拉里少一个文档」。"""
    s = 0.0
    name = c['head_name'] or ''
    if name and not name.lower().endswith(('.sai', '.sai2')):
        s += 3.0
    if not any(ch.isalnum() or '\u4e00' <= ch <= '\u9fff' for ch in name):
        s -= 2.0
    s += min(c['chain_len'], 5) * 0.8
    if c['chain_len'] >= 2:
        s += 2.0
    if c['layout'] == 'base':
        s += 0.5
    return s


def dedupe(cands):
    best = {}
    for c in cands:
        cur = best.get(c['offset'])
        if cur is None:
            best[c['offset']] = dict(c)
            continue
        if c['chain_len'] > cur['chain_len']:
            best[c['offset']] = dict(c)
    return list(best.values())


# ---------------------------------------------------------------- 缓存
def save_cache(exe_hash, name, offset, layout):
    """把命中结果并进用户级外部版本表。

    合并而不是覆盖：这份文件里可能已经有用户/作者手写的其他条目，直接盖掉
    等于让人家的适配凭空消失。文件坏了就改名留档再重建，别让一条探测成功
    反过来把整个版本表弄丢。
    """
    data = {'schema': EXTERNAL_SCHEMA, 'versions': []}
    if CACHE_PATH.exists():
        try:
            raw = json.loads(CACHE_PATH.read_text(encoding='utf-8-sig'))
            if isinstance(raw, dict) and isinstance(raw.get('versions'), list):
                data = raw
                data['schema'] = EXTERNAL_SCHEMA
        except Exception:
            try:
                CACHE_PATH.replace(CACHE_PATH.with_suffix('.json.bad'))
            except Exception:
                pass

    entry = {'hash': exe_hash, 'name': name, 'offset': hex(offset), 'layout': layout}
    if layout == 'new':
        entry['fields'] = NEW_FIELDS
    versions = [v for v in data.get('versions', [])
                if isinstance(v, dict) and str(v.get('hash', '')).lower() != exe_hash.lower()]
    versions.append(entry)
    data['versions'] = versions
    CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding='utf-8')
    return CACHE_PATH


# ---------------------------------------------------------------- 总入口
def detect(pid, exe_hash, version_name, log=None, timeout=20.0, save=True):
    """跑三层识别。成功时把类注册进 sai_api_lookup，并（默认）写缓存。

    ``save=False`` 只探测不落盘 —— 测试脚本用它，免得把假 MD5 写进用户的
    版本表；生产路径不要传 False，否则下次启动还得再探一次。

    返回 dict：ok / offset / layout / stage / cls / message / elapsed / cached。
    这个函数是**同步阻塞**的，调用方必须放到后台线程（见 webapp 的
    _auto_detect_worker）—— 它最长会跑满 timeout 秒。
    """
    log = log or logging.info
    t0 = time.monotonic()
    deadline = t0 + timeout
    out = {'ok': False, 'offset': None, 'layout': None, 'stage': None,
           'cls': None, 'message': '', 'elapsed': 0.0, 'cached': False,
           'canvas_count': 0, 'head_name': ''}

    # 没有合法 MD5 就别探了。探测出来的结果要靠 hash 登记，hash 是空的
    # 就只能用这一次 —— 下次启动照样不认识，用户会以为「自动适配时好时坏」。
    # 更糟的是缓存：一条 hash=000…0 的条目会永久躺在用户的版本表里。
    # 拿不到 MD5 基本等于拿不到 exe 路径（权限问题），探测也难成功。
    if not re.fullmatch(r'[0-9a-f]{32}', (exe_hash or '').lower()) or not exe_hash.strip('0'):
        out['message'] = '拿不到 SAI2 的 exe 指纹（MD5），无法登记适配结果'
        out['elapsed'] = round(time.monotonic() - t0, 2)
        return out
    exe_hash = exe_hash.lower()

    # 第 1 层的兜底：调用方通常先查过表，但测试脚本、以及「探测完又重启」
    # 这类路径会直接进来。已经在表里就别再探一遍，更别把它写进用户的缓存 ——
    # 内置条目被外部条目覆盖一次，等于把「内置验证过的类」换成「动态造的类」。
    existing = sai.sai_api_lookup.get(exe_hash)
    if existing is not None:
        out.update(ok=True, offset=int(existing.session_offset),
                   layout=layout_of(existing), stage='lookup', cls=existing,
                   message='这个版本已经在版本表里，直接使用')
        out['elapsed'] = round(time.monotonic() - t0, 2)
        return out

    try:
        proc_ctx = OpenProcess(pid=pid)
    except Exception as exc:
        out['message'] = f'打不开 SAI2 进程（{type(exc).__name__}）'
        out['elapsed'] = round(time.monotonic() - t0, 2)
        return out

    with proc_ctx as proc:
        # ---- 第 2 层：已知偏移试探
        combos = rank_combos(known_combos(exclude_hash=exe_hash), version_name)
        log(f'未登记的 SAI2 版本，开始自动适配：先试 {len(combos)} 个已验证过的偏移')
        best_fail = None
        for c in combos:
            if time.monotonic() > deadline:
                out['message'] = '自动适配超时'
                break
            res = verify(proc, c['offset'], c['layout'], exe_hash, version_name, deadline)
            state = 'OK' if res['ok'] else (res['error'] or '失败')
            log(f"  试 {hex(c['offset'])} {c['layout']}（{c['names'][0]}）-> {state}")
            if res['ok']:
                out.update(ok=True, offset=c['offset'], layout=c['layout'],
                           stage='known', canvas_count=res['canvas_count'],
                           head_name=res['head_name'])
                break
            if res['canvas_count'] == 0 and ('占位' in str(res['error'])):
                best_fail = best_fail or res

        # ---- 第 3 层：结构扫描
        if not out['ok'] and time.monotonic() <= deadline:
            log('已知偏移全部没命中，开始结构扫描（这一步要读几 MB 内存，慢一点）')
            modules = list(proc.get_modules())
            mod = next((m for m in modules
                        if (m.name or '').lower() == 'sai2.exe'), None)
            if mod is None:
                out['message'] = '拿不到 sai2.exe 的模块信息'
            else:
                cands = dedupe(scan_offsets(proc, mod.base_address, mod.size,
                                            log, deadline))
                for c in cands:
                    c['score'] = round(_score(c), 2)
                cands.sort(key=lambda c: -c['score'])
                log(f'结构扫描找到 {len(cands)} 个候选，逐个验证')
                for c in cands[:10]:
                    if time.monotonic() > deadline:
                        out['message'] = '自动适配超时'
                        break
                    res = verify(proc, c['offset'], c['layout'], exe_hash,
                                 version_name, deadline)
                    state = 'OK' if res['ok'] else (res['error'] or '失败')
                    log(f"  {hex(c['offset'])} {c['layout']}（头 {c['head_name']!r}）-> {state}")
                    if res['ok']:
                        out.update(ok=True, offset=c['offset'], layout=c['layout'],
                                   stage='scan', canvas_count=res['canvas_count'],
                                   head_name=res['head_name'])
                        break
                    if res['canvas_count'] == 0 and ('占位' in str(res['error'])):
                        best_fail = best_fail or res

        if not out['ok']:
            if best_fail is not None:
                out['message'] = ('已经能读到画布链表，但链上只有占位节点 —— '
                                  '在 SAI2 里打开任意一张画，程序会自动重试。')
            elif not out['message']:
                out['message'] = ('没能自动识别这个 SAI2 版本。请用 Sai2-Sniffer '
                                  '生成报告发到 QQ 群 942992768。')
            out['elapsed'] = round(time.monotonic() - t0, 2)
            return out

        # ---- 命中：造类、注册、缓存
        # 名字以 exe 的版本资源为准。调用方传进来的多半是「未识别版本」，
        # 那条字符串会进用户的版本表、进下拉、进日志，写错了以后很难追。
        version_name = display_name(pid, exe_hash, version_name)
        spec = {'hash': exe_hash, 'name': version_name,
                'offset': hex(out['offset']), 'layout': out['layout']}
        if out['layout'] == 'new':
            spec['fields'] = NEW_FIELDS
        try:
            cls = sai.make_external_version_class(spec)
            sai.sai_api_lookup[exe_hash] = cls
            out['cls'] = cls
        except Exception as exc:
            out['ok'] = False
            out['message'] = f'参数能读，但注册失败：{type(exc).__name__}: {exc}'
            out['elapsed'] = round(time.monotonic() - t0, 2)
            return out

        try:
            if save:
                save_cache(exe_hash, version_name, out['offset'], out['layout'])
                out['cached'] = True
        except Exception as exc:
            log(f'适配成功但写缓存失败（{type(exc).__name__}），本次运行仍然可用')

        out['message'] = (f"已自动适配当前 SAI2 版本（偏移 {hex(out['offset'])}，"
                          f"{out['layout']} 布局），"
                          f"{'已保存到本地版本表' if out['cached'] else '本次运行有效'}。"
                          f"请先试录一小段确认画面正常。")
        out['elapsed'] = round(time.monotonic() - t0, 2)
        return out
