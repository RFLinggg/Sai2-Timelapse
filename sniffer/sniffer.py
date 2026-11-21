#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Sai2-Sniffer —— 读取用户机器上 SAI2 的适配参数，产出一份可直接发回来的报告。

为什么需要它：Sai2-Timelapse 靠 exe MD5 精确匹配版本表，表里没有的版本只能靠
用户手动在下拉里挑一个「差不多」的套上去 —— 而那会把画布读成空帧或花屏，
日志里一个错都没有。要让作者支持一个新版本，需要三样东西：

  1. exe 的 MD5（唯一键，光看版本号没用：同一个 2024.05.22 有日文原版、
     [Saier] 汉化版等不同编译，offset 各不相同）
  2. session_offset（画布链表指针在模块里的偏移）
  3. 这个 offset 配上当前布局**到底能不能读出图**（读不出就是布局变了）

本工具自己算前两样，并强制验证第三样（真的读一张画布存成 PNG）。PNG 是给作者
肉眼判定的证据，比任何"探测成功"的字符串都可靠。

探测不依赖任何字符串锚点（上游脚本依赖「便签薄」，日文版/英文版就没有）。
做法是批量扫模块映像里的指针槽，用「节点页对齐 + 宽高合理 + next_canvas 合法 +
名字能解码成可读文本」做结构验证，再用主程序同一套 SAIv2_API_Base 真读一张图。

用法：
    python sniffer.py                 # 图形界面（给用户）
    python sniffer.py --cli           # 命令行，直接出报告（排错 / 自检）
    python sniffer.py --cli --out DIR # 指定报告输出目录
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import queue
import struct
import sys
import threading
import time
import traceback
import zlib
from ctypes import wintypes
from pathlib import Path

import numpy as np

SNIFFER_VERSION = '1.05'

# ---------------------------------------------------------------- 包导入
# 源码运行时把 ../src 加进 sys.path；打包后 art_timelapse 已经在包里。
def _ensure_package_importable() -> None:
    if getattr(sys, 'frozen', False):
        return
    here = Path(__file__).resolve()
    for candidate in (here.parent.parent / 'src', here.parent / 'src'):
        if (candidate / 'art_timelapse' / 'sai.py').exists():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return


_ensure_package_importable()
import art_timelapse.sai as sai  # noqa: E402  （必须在上面的路径处理之后）


# ---------------------------------------------------------------- 画布布局
# (name_off, width_off, height_off, id_off, label, 是否基址布局)
# next_canvas 在所有已知布局里都是 0x000。
LAYOUTS = {
    'base': dict(name=0x2ac, w=0x250, h=0x254, cid=0x020,
                 label='base (2023.07.11 / 2024.05.22 / 2024.11.23)'),
    'new': dict(name=0x8f8, w=0x028, h=0x02c, cid=None,
                label='new (2026.09.25 alpha 及以后的大重构)'),
}

# new 布局的字段表：offset 对了也必须覆盖这些，否则粘进 sai.py 依然读不出图。
NEW_CANVAS_FIELDS = [
    (0x000, 'next_canvas', None),      # 类型在 build 时填
    (0x008, 'prev_canvas', None),
    (0x010, 'prev_user_canvas', None),
    (0x018, 'next_user_canvas', None),
    (0x028, 'width', ctypes.c_int32),
    (0x02c, 'height', ctypes.c_int32),
    (0x048, 'tile_maps', None),
    (0x180, 'tile_count_x', ctypes.c_uint32),
    (0x184, 'tile_count_y', ctypes.c_uint32),
    (0x8f8, 'name', ctypes.c_uint16 * 0x104),
    (0xb00, 'short_path', ctypes.c_uint16 * 0x104),
]

MAX_NODES = 64
PAGE = 0x1000


# ---------------------------------------------------------------- exe 版本资源
def read_version_info(path: str) -> dict:
    """读 exe 的 VS_VERSIONINFO（不用 pywin32，纯 ctypes，几十毫秒）。

    这些值只给人看 —— 判断是不是同一个编译要看 MD5。
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

    def query(sub: str):
        if not ver.VerQueryValueW(buf, ctypes.c_wchar_p(sub),
                                  ctypes.byref(ptr), ctypes.byref(length)):
            return None
        if not ptr.value or not length.value:
            return None
        # 不能信 length：SAI 的版本资源把 value 长度字段按「字符数」而不是
        # 「字节数」写了（FileDescription 报 47，实际 46 字符 = 92 字节），
        # VerQueryValueW 老实照做就把字符串截成半截。改成读到双字节 null 为止。
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

    keys = ('FileDescription', 'FileVersion', 'ProductVersion', 'ProductName',
            'CompanyName', 'LegalCopyright', 'OriginalFilename')
    for tr in translations:
        got = {k: query(f'\\StringFileInfo\\{tr}\\{k}') for k in keys}
        if any(v for v in got.values()):
            out.update({k: v for k, v in got.items() if v})
            out['translation'] = tr
            break
    fixed = query('\\')
    return out


def file_md5(path: str) -> str | None:
    try:
        with open(path, 'rb') as f:
            return hashlib.file_digest(f, 'md5').hexdigest()
    except OSError:
        return None


def collect_exe_info(pid: int) -> dict:
    """连进程都没打开就能拿到的全部信息。这一层 100% 可靠。"""
    info = {'pid': pid}
    try:
        import psutil
        proc = psutil.Process(pid)
        info['exe_path'] = proc.exe()
        info['process_name'] = proc.name()
        info['started_at'] = time.strftime('%Y-%m-%d %H:%M:%S',
                                           time.localtime(proc.create_time()))
    except Exception as exc:
        info['exe_path'] = None
        info['error'] = f'{type(exc).__name__}: {exc}'
        return info

    path = info['exe_path']
    try:
        st = Path(path).stat()
        info['exe_size'] = st.st_size
        info['exe_modified'] = time.strftime('%Y-%m-%d %H:%M:%S',
                                             time.localtime(st.st_mtime))
    except OSError:
        pass
    info['exe_md5'] = file_md5(path)
    info['version_info'] = read_version_info(path)
    known = sai.get_sai_api(info['exe_md5']) if info['exe_md5'] else None
    info['known_to_timelapse'] = bool(known)
    info['known_as'] = known.version_name if known else None
    return info


# ---------------------------------------------------------------- 内存小工具
def _u64(proc, addr: int) -> int:
    try:
        return int.from_bytes(proc.read_bytes(addr, 8), 'little')
    except Exception:
        return 0


def _i32(proc, addr: int) -> int:
    try:
        return int.from_bytes(proc.read_bytes(addr, 4), 'little', signed=True)
    except Exception:
        return -1


def _valid_ptr(v: int) -> bool:
    return 0x10000 <= v < 0x0000800000000000


def _decode_name(proc, node: int, name_off: int, limit: int = 0x100) -> str:
    try:
        raw = proc.read_bytes(node + name_off, limit)
    except Exception:
        return ''
    text = raw.decode('utf-16le', 'ignore')
    for sep in ('\x00', '\uffff'):
        if sep in text:
            text = text.split(sep)[0]
    return text.strip()


def _walk(proc, head: int) -> list[int]:
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


def _heap_regions(proc, base: int, size: int) -> list[tuple[int, int]]:
    """模块映像之外的可读区（堆）。结构扫描只在这些范围里找指针。"""
    out = []
    try:
        for r in proc.get_memory_regions():
            if not r.is_readable:
                continue
            if r.address < base + size and r.address + r.size > base:
                continue                      # 跳过模块映像自身
            out.append((r.address, r.address + r.size))
    except Exception:
        pass
    return out


def _read_module(proc, base: int, size: int, limit: int = 16 * 1024 * 1024) -> bytes:
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
            data = data + b'\x00' * (PAGE - len(data))
        chunks.append(data)
        addr += PAGE
    return b''.join(chunks)


# ---------------------------------------------------------------- 候选发现
def _candidate(offset: int, node: int, layout: str, method: str,
               head_name: str, w: int, h: int) -> dict:
    return {
        'session_offset': offset,
        'node': node,
        'layout': layout,
        'method': method,
        'head_name': head_name,
        'width': w,
        'height': h,
        'chain_len': 1,
    }


def scan_anchored(proc, base: int, anchors: list[str], log) -> list[dict]:
    """老办法：搜占位画布名 → 反推节点 → 反查指向它的指针槽。

    只在占位画布名恰好是这些词之一时有效，当兜底/交叉验证用。
    """
    found = []
    for anchor in anchors:
        pattern = ' '.join('%02X' % b for b in anchor.encode('utf-16le'))
        try:
            hits = list(proc.search_by_pattern(pattern))
        except Exception as exc:
            log(f'  锚点 {anchor} 搜索失败：{exc}')
            continue
        if not hits:
            continue
        log(f'  锚点 {anchor}：字符串命中 {len(hits)} 处')
        for addr in hits:
            for key, lay in LAYOUTS.items():
                node = addr - lay['name']
                if node <= 0:
                    continue
                w, h = _i32(proc, node + lay['w']), _i32(proc, node + lay['h'])
                if not (0 < w < 0xffff and 0 < h < 0xffff):
                    continue
                ptr = ' '.join('%02X' % b for b in node.to_bytes(8, 'little'))
                try:
                    slots = list(proc.search_by_pattern(ptr))
                except Exception:
                    continue
                for slot in slots:
                    rel = slot - base
                    if not (0 <= rel < 0x1000000) or _u64(proc, base + rel) != node:
                        continue
                    cand = _candidate(rel, node, key, 'anchor', anchor, w, h)
                    cand['chain_len'] = len(_walk(proc, node))
                    found.append(cand)
    return found


def scan_structural(proc, base: int, size: int, log) -> list[dict]:
    """不依赖任何字符串：批量扫模块映像里的指针槽，用结构特征筛。

    筛条件（全部满足才算候选）：
      · 槽里存的值落在可读堆区
      · 节点地址页对齐（SAI 的画布节点是页分配的，这一条砍掉九成噪声）
      · 节点 +宽/高 读出来是合理正整数
      · 节点 next_canvas 是 0 或合法指针
      · 节点名字能解码成可打印文本
    """
    regions = _heap_regions(proc, base, size)
    if not regions:
        log('  拿不到堆区列表，跳过结构扫描')
        return []

    log(f'  堆区 {len(regions)} 段，批量读模块映像…')
    raw = _read_module(proc, base, size)
    vals = np.frombuffer(raw[:len(raw) // 8 * 8], dtype='<u8')
    addrs = base + np.arange(len(vals), dtype=np.uint64) * 8

    mask = np.zeros(len(vals), dtype=bool)
    for lo, hi in regions:
        mask |= (vals >= np.uint64(lo)) & (vals < np.uint64(hi))
    cand_addr = addrs[mask]
    cand_val = vals[mask]
    log(f'  模块内 {len(vals)} 个 8 字节槽，落在堆区的 {len(cand_val)} 个')

    found = []
    for key, lay in LAYOUTS.items():
        for addr, val in zip(cand_addr, cand_val):
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
            cand = _candidate(rel, node, key, 'structure', name, w, h)
            cand['chain_len'] = len(_walk(proc, node))
            found.append(cand)
    return found


def dedupe(cands: list[dict]) -> list[dict]:
    """同一个 offset 可能被两种方法、两个布局同时命中，合并保留信息最全的。"""
    best: dict[int, dict] = {}
    for c in cands:
        key = c['session_offset']
        cur = best.get(key)
        if cur is None:
            best[key] = dict(c)
            continue
        cur['chain_len'] = max(cur['chain_len'], c['chain_len'])
        if cur['method'] == 'structure' and c['method'] == 'anchor':
            cur['method'] = 'anchor+structure'
        if c['chain_len'] > cur['chain_len'] or not cur['head_name']:
            cur['head_name'] = c['head_name'] or cur['head_name']
            cur['node'] = c['node']
            cur['width'] = c['width']
            cur['height'] = c['height']
            cur['layout'] = c['layout']
    return list(best.values())


def score(c: dict) -> float:
    """给候选打分。选错 offset 的后果不是崩，是画布下拉里少一个文档 —— 很难发现，
    所以这里宁可保守：链表头是「不带扩展名的占位画布」这条权重最高，
    因为它正是主程序 get_canvas_list() 用 [1:] 丢掉第一个节点的语义来源。"""
    s = 0.0
    name = c['head_name'] or ''
    if name and not name.lower().endswith(('.sai', '.sai2')):
        s += 3.0
    if not any(ch.isalnum() or '\u4e00' <= ch <= '\u9fff' for ch in name):
        s -= 2.0                       # 「▯」这类纯符号，基本是误命中
    s += min(c['chain_len'], 5) * 0.8
    if c['chain_len'] >= 2:
        s += 2.0
    if c['layout'] == 'base':
        s += 0.5                       # base 布局是目前实际在用的主流
    if 'anchor' in c['method']:
        s += 0.5
    return s


# ---------------------------------------------------------------- 验证
def build_api_class(offset: int, layout: str, version_name: str, exe_hash: str):
    """动态造一个 API 类，和 sai.py 里手写的那些等价 —— 用主程序的读法验证，
    这样「嗅探器读得出来」才等价于「主程序录得出来」。"""
    attrs = {
        'version_name': version_name,
        'exe_hash': exe_hash,
        'session_offset': offset,
    }
    if layout == 'new':
        canvas = type('SAICanvas', (sai.SAIv2CanvasBase,), {})
        rp = sai.RPOINTER
        tile_map = sai.SAIv2_API_Base.SAICanvasTileMap
        fields = []
        for off, name, typ in NEW_CANVAS_FIELDS:
            if name in ('next_canvas', 'prev_canvas', 'prev_user_canvas',
                        'next_user_canvas'):
                typ = rp(canvas)
            elif name == 'tile_maps':
                typ = rp(rp(tile_map)) * sai.SAIv2_API_Base.map_count
            fields.append((off, name, typ))
        canvas._fields_ = sai.offset_fields(fields)
        attrs['SAICanvas'] = canvas
        attrs['check_if_canvas_exists'] = sai.SAIv2_API_Base.check_if_canvas_exists
    return type('SniffedSAIv2', (sai.SAIv2_API_Base,), attrs)


def pick_coarse_level(api, canvas, hi: int = 1200) -> int:
    """挑一级 tile map 用于出图。

    不取最粗的一级 —— 7000×7000 的画布最粗那级只有 7×7 像素，当证据等于没有。
    目标是「不超过 hi 像素里最大的那一级」，这样既快又能看清画面。
    """
    levels = []
    for i in range(sai.SAIv2_API_Base.map_count):
        try:
            tm = canvas.tile_maps[i].get(api.proc).get(api.proc)
        except Exception:
            break
        if tm.width > 0 and tm.height > 0:
            levels.append((i, tm.width, tm.height))
    if not levels:
        return 0
    for i, w, h in levels:          # i 递增、尺寸递减，第一个够小的就是最清楚的
        if max(w, h) <= hi:
            return i
    return levels[-1][0]


# 有效画布的最小边长。SAI 建不出这么小的画布（最小也是几百像素），
# 读到比这更小的一律当「offset 指错了」，而不是「读到了小画布」。
MIN_CANVAS_SIDE = 8
# 读出来的图宽高比和画布字段允许差多少。2% 足够吸收缩略图取整的误差，
# 又能抓住「4000x3000 读出 1800x750」这种真正错位的（差 80%）。
RATIO_TOLERANCE = 0.02


def verify_candidate(proc, cand: dict, exe_hash: str, version_name: str,
                     png_path: Path | None = None, log=print) -> dict:
    """按候选造类 → 列画布 → 真读一张图。返回的结果就是最终判据。"""
    result = {'ok': False, 'error': None, 'canvases': [], 'level': None}
    try:
        cls = build_api_class(cand['session_offset'], cand['layout'],
                              version_name, exe_hash)
        api = cls(proc)
        canvases = api.get_canvas_list()
        result['canvas_count'] = len(canvases)
        if not canvases:
            result['error'] = '画布列表为空（offset 指向的链表可能只有占位画布）'
            return result

        for canvas in canvases[:4]:
            entry = {}
            try:
                entry['name'] = canvas.get_name()
            except Exception:
                entry['name'] = '?'
            try:
                entry['short_path'] = canvas.get_short_path()
            except Exception:
                entry['short_path'] = ''
            try:
                entry['size'] = [int(canvas.width), int(canvas.height)]
            except Exception:
                entry['size'] = None
            entry['level'] = pick_coarse_level(api, canvas)
            try:
                img = api.get_canvas_image(canvas, entry['level'])
                arr = np.asarray(img)
                entry['array_shape'] = list(arr.shape)
                h = arr.shape[0] if arr.ndim >= 1 else 0
                w = arr.shape[1] if arr.ndim >= 2 else 0
                # 光「没抛异常」不算读成功。offset 指错时 get_canvas_image 常常
                # 返回 0×0 的空数组，np.asarray 一样不报错 —— 之前这里直接
                # entry['read']=True，于是一个链长 64、画布尺寸 1×13 的垃圾候选
                # 被判成「OK」，后面 12 个候选再也没试过（真实案例见 2026-11-18
                # 那份 1a65a72f 报告）。
                entry['read'] = bool(w >= MIN_CANVAS_SIDE and h >= MIN_CANVAS_SIDE)
                if not entry['read']:
                    entry['error'] = f'读出空图 {w}x{h}'
                else:
                    # 尺寸够大还不够 —— 宽高比也得对得上。画布字段说 4000x3000，
                    # 读出来却是 1800x750，说明 width/height 和 tile 结构里有一边
                    # 指错了，导出的成片会是拉伸变形的。2026-11-19 那份
                    # Alpha.2026.10.19 的报告就是这样被误判成「成功」的。
                    declared = None
                    if entry['size']:
                        cw, ch = entry['size']
                        if cw > 0 and ch > 0:
                            declared = cw / ch
                    if declared:
                        actual = w / h
                        if abs(actual - declared) / declared > RATIO_TOLERANCE:
                            entry['read'] = False
                            entry['error'] = (
                                f'宽高比不对：画布 {entry["size"][0]}x{entry["size"][1]}，'
                                f'读出来 {w}x{h}')
                    if entry['read']:
                        entry['mean'] = round(float(arr.mean()), 2)
                        entry['std'] = round(float(arr.std()), 2)
                        entry['non_black_ratio'] = round(float((arr > 8).mean()), 4)
                result['canvases'].append(entry)
                if entry['read'] and png_path is not None and not result.get('png'):
                    write_png(png_path, arr)
                    result['png'] = png_path.name
            except Exception as exc:
                entry['read'] = False
                entry['error'] = f'{type(exc).__name__}: {exc}'
                result['canvases'].append(entry)

        result['ok'] = any(c.get('read') for c in result['canvases'])
        if not result['ok']:
            result['error'] = '画布读不出来（布局与 offset 不匹配）'
        return result
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        return result


def dump_node(proc, address: int, size: int = 0x1000) -> str | None:
    """把候选节点的前 4KB dump 成 hex。

    扫描全线失败时，这一小段内存比整个 exe 有用得多：适配真正需要的是**运行时**
    的字段布局，而 exe 里只有代码，拿到手也推不出来（还得自己在 IDA 里找那个
    全局对象）。原来只 dump 256 字节，能看到指针和尺寸，但 name 字段在 0x8f8，
正好差一点够不着 —— 而 name 是判断「这到底是不是画布」最直接的东西。
2026-11-19 那几份失败报告就是因为看不到 name 才白跑一轮。
    又小到不含任何软件本体。
    """
    try:
        data = proc.read_bytes(address, size)
    except Exception:
        return None
    if not data:
        return None
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'{i:04x}  {hex_part:<47}  {ascii_part}')
    return '\n'.join(lines)


# ---------------------------------------------------------------- PNG 输出
def write_png(path: Path, rgb: np.ndarray) -> None:
    """手写最小 PNG 编码器。为一张缩略图拉 83MB 的 opencv 不值当，
    而且 cv2.imwrite 在中文路径下还会静默失败。"""
    arr = np.asarray(rgb)
    if arr.ndim == 3 and arr.shape[2] >= 3:
        arr = arr[:, :, :3]
    elif arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=2)
    else:
        raise ValueError(f'unsupported array shape {arr.shape}')
    arr = np.ascontiguousarray(arr.astype(np.uint8))
    h, w = arr.shape[:2]
    raw = b''.join(b'\x00' + arr[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))

    blob = b'\x89PNG\r\n\x1a\n'
    blob += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    blob += chunk(b'IDAT', zlib.compress(raw, 6))
    blob += chunk(b'IEND', b'')
    Path(path).write_bytes(blob)


# ---------------------------------------------------------------- 主流程
def scan(pid: int | None = None, log=print, progress=None,
         out_dir: Path | None = None) -> dict:
    """完整扫描。返回报告 dict（同时落盘 report.json / canvas.png）。"""
    t_start = time.time()
    report = {
        'schema': 1,
        'sniffer_version': SNIFFER_VERSION,
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'machine': {'os': sys.getwindowsversion().platform_version
                    if hasattr(sys, 'getwindowsversion') else sys.platform,
                    'python': sys.version.split()[0]},
        'sai': {},
        'detected': None,
        'verified': None,
        'candidates': [],
        'timeline': [],
        'errors': [],
    }

    def step(text):
        stamp = round(time.time() - t_start, 2)
        report['timeline'].append({'t': stamp, 'text': text})
        log(f'[{stamp:5.2f}s] {text}')
        if progress:
            progress(text)

    step('查找 SAI 进程')
    if pid is None:
        pid = sai.find_running_sai_pid()
    if not pid:
        msg = '没找到 sai2.exe / sai.exe。请先打开 SAI2，并至少打开一个文档。'
        report['errors'].append(msg)
        step(msg)
        return report

    info = collect_exe_info(pid)
    report['sai'] = info
    step(f"pid={pid}  {info.get('exe_path')}")
    vi = info.get('version_info') or {}
    step(f"版本资源：{vi.get('FileDescription') or vi.get('ProductVersion') or '读不到'}")
    step(f"MD5：{info.get('exe_md5')}  "
         f"{'已在版本表里' if info.get('known_to_timelapse') else '不在版本表里'}")

    if (info.get('process_name') or '').lower() == 'sai.exe':
        report['errors'].append('检测到的是 SAI Ver.1（sai.exe）。本工具只支持 SAI2。')
        step('是 SAI1，不支持')
        return report

    try:
        from PyMemoryEditor import OpenProcess
        with OpenProcess(pid=pid) as proc:
            step('打开进程')
            modules = list(proc.get_modules())
            if not modules:
                raise RuntimeError('拿不到模块列表（权限不足？请用管理员运行试试）')
            mod = next((m for m in modules
                        if (m.name or '').lower() == 'sai2.exe'), modules[0])
            base, size = mod.base_address, mod.size
            step(f'模块 {mod.name} base={base:#x} size={size:#x}')

            step('结构扫描（不依赖任何字符串锚点）')
            cands = scan_structural(proc, base, size, log)

            # 字符串锚点法每个锚点都要全内存搜一遍（本机 9 个锚点要 35 秒），
            # 只能当兜底：结构扫描连一个多节点链表都没找到时才跑。
            if not any(c['chain_len'] >= 2 for c in cands):
                step('结构扫描没找到像样的链表，回退字符串锚点')
                anchors = ['便签薄', 'Notes', 'メモ']
                cands += scan_anchored(proc, base, anchors, log)

            cands = dedupe(cands)
            for c in cands:
                c['score'] = round(score(c), 2)
            cands.sort(key=lambda c: (-c['score'], c['session_offset']))
            step(f'候选 {len(cands)} 个，按可信度排序')

            for c in cands[:12]:
                step(f"  off={c['session_offset']:#x} {c['layout']:4s} "
                     f"chain={c['chain_len']} score={c['score']} "
                     f"head={c['head_name']!r}")

            if not cands:
                report['errors'].append('没有找到候选 offset。可能是布局大改，'
                                        '或者 SAI2 里没有打开的文档。')
                step('候选为空')
                return report

            out_dir = Path(out_dir) if out_dir else Path.cwd()
            out_dir.mkdir(parents=True, exist_ok=True)
            png_path = out_dir / 'canvas.png'

            version_name = (vi.get('FileDescription')
                            or f"SAI Ver.2 {vi.get('ProductVersion', 'unknown')}")
            # 落在模块映像里的候选不是堆对象 —— 那是 DLL/EXE 的 PE 头。
            # 2026-11-19 那份失败报告里 5 个候选有 3 个是这种（dump 开头全是 "MZ"），
            # 既浪费时间又干扰判断。注意别全滤光：万一过滤逻辑出错，
            # 保留原始候选至少还能试。
            def _module_owner(addr):
                for m in modules:
                    if m.base_address <= addr < m.base_address + m.size:
                        return m.name
                return None

            kept = []
            skipped = 0
            for c in cands:
                if _module_owner(c['node']):
                    skipped += 1
                else:
                    kept.append(c)
            if skipped:
                step(f'  跳过 {skipped} 个落在模块映像里的候选（不是堆对象）')
            if kept:
                cands = kept

            step('逐个验证（真读一张画布）')
            chosen = None
            for c in cands[:8]:
                res = verify_candidate(proc, c, info.get('exe_md5') or '',
                                       version_name, png_path, log)
                c['verify'] = res
                state = 'OK' if res.get('ok') else (res.get('error') or 'fail')
                step(f"  off={c['session_offset']:#x} -> {state}"
                     f"（画布 {res.get('canvas_count', 0)} 个）")
                if res.get('ok') and chosen is None:
                    chosen = c
                    break
            if chosen is None:
                report['candidates'] = cands[:12]
                # 「能读到链表但 [1:] 之后为空」和「读不到图」是两回事：
                # 前者说明 offset 已经指对了，只是 SAI2 里没打开文档 —— 打开一张画
                # 再扫一次就行；后者才是布局真的变了，得等作者适配。
                # 以前两种情况都报「布局可能真的变了」，把用户往错误方向带。
                placeholder = None
                for c in cands:
                    err = str((c.get('verify') or {}).get('error') or '')
                    if '画布列表为空' in err:
                        placeholder = c
                        break
                if placeholder is not None:
                    report['errors'].append(
                        f"SAI2 里没有打开的文档。offset 0x{placeholder['session_offset']:x} "
                        f"已经能读到画布链表，但链上只有占位节点"
                        f"（{placeholder['head_name']!r}）——"
                        f"在 SAI2 里打开一张画，再扫一次。")
                else:
                    report['errors'].append(
                        '所有候选都读不出画布。布局可能真的变了，需要人工分析。')
                    # 最常见的低级原因就是「SAI2 开着但没开画」——那时画布链表里
                    # 只有占位节点，扫描只能找到别的 64 节点垃圾链。
                    # 上面那条 placeholder 检测只管「链上只有占位节点」，
                    # 遇到垃圾长链就漏了，所以这里补一句让用户自己核对。
                    report['errors'].append(
                        '请先确认：扫描时 SAI2 里是否打开了至少一张画？'
                        '空窗口（只有新建标签、没打开文档）扫出来的都是垃圾。')
                # 全线失败时把候选的节点内存 dump 出来 —— 下次遇到陌生版本，
                # 有这段字节就能离线判断布局，不用再让用户来回扫。
                #
                # 数量定 10 而不是 5：2026-11-19 那份 da5b98fa 报告里，9 个候选
                # 只有最后一个（chain=1、offset 0x2c7c70）看起来像真正的链表头，
                # 但它 score 最低排在最后，只 dump 前 5 个正好把它漏掉 —— 白跑一轮。
                # chain=1 恰恰是「偏移对了、但还没打开画布」的典型形态，最该看。
                dumps = []
                for c in cands[:10]:
                    text = dump_node(proc, c['node'])
                    if text:
                        dumps.append({
                            'offset': f"0x{c['session_offset']:x}",
                            'layout': c['layout'],
                            'node': f"0x{c['node']:x}",
                            'head_name': c['head_name'],
                            'chain_len': c['chain_len'],
                            'score': c['score'],
                            'bytes': 0x1000,
                            'hex': text,
                        })
                if dumps:
                    report['node_dumps'] = dumps
                    step(f'  已 dump {len(dumps)} 个候选节点的前 4KB')
                step('验证全部失败')
                return report

            report['detected'] = {
                'layout': chosen['layout'],
                'layout_label': LAYOUTS[chosen['layout']]['label'],
                'session_offset': f"0x{chosen['session_offset']:x}",
                'session_offset_int': chosen['session_offset'],
                'method': chosen['method'],
                'score': chosen['score'],
                'head_node': f"0x{chosen['node']:x}",
                'head_name': chosen['head_name'],
                'chain_len': chosen['chain_len'],
            }
            report['verified'] = chosen['verify']
            report['candidates'] = cands[:12]
            report['class_snippet'] = render_snippet(chosen, version_name,
                                                     info.get('exe_md5') or '')
            step(f"最终：session_offset = 0x{chosen['session_offset']:x}"
                 f"  布局 {chosen['layout']}")

    except Exception as exc:
        report['errors'].append(f'{type(exc).__name__}: {exc}')
        report['traceback'] = traceback.format_exc()
        step(f'出错：{exc}')

    report['elapsed'] = round(time.time() - t_start, 2)
    return report


def render_snippet(cand: dict, version_name: str, exe_hash: str) -> str:
    """生成可以直接粘进 src/art_timelapse/sai.py 的类定义。"""
    lines = [
        '@register_sai_api',
        f'class SAIv2_API_{exe_hash[:8]}(SAIv2_API_Base):',
        f"    version_name = '{version_name}'",
        f"    exe_hash = '{exe_hash}'",
        f"    session_offset = 0x{cand['session_offset']:x}",
    ]
    if cand['layout'] == 'new':
        lines += [
            '',
            '    class SAICanvas(SAIv2CanvasBase):',
            '        pass',
            '',
            '    SAICanvas._fields_ = offset_fields([',
            "            (0x000, 'next_canvas', RPOINTER(SAICanvas)),",
            "            (0x008, 'prev_canvas', RPOINTER(SAICanvas)),",
            "            (0x010, 'prev_user_canvas', RPOINTER(SAICanvas)),",
            "            (0x018, 'next_user_canvas', RPOINTER(SAICanvas)),",
            "            (0x028, 'width', ctypes.c_int32),",
            "            (0x02c, 'height', ctypes.c_int32),",
            "            (0x048, 'tile_maps', RPOINTER(RPOINTER(SAIv2_API_Base.SAICanvasTileMap)) * SAIv2_API_Base.map_count),",
            "            (0x180, 'tile_count_x', ctypes.c_uint32),",
            "            (0x184, 'tile_count_y', ctypes.c_uint32),",
            "            (0x8f8, 'name', ctypes.c_uint16 * 0x104),",
            "            (0xb00, 'short_path', ctypes.c_uint16 * 0x104)",
            '    ])',
            '',
            '    def check_if_canvas_exists(self, canvas):',
            '        return SAI_API_Base.check_if_canvas_exists(self, canvas)',
        ]
    return '\n'.join(lines)


def write_report(report: dict, out_dir: Path) -> Path:
    """把 report.json + canvas.png + 说明.txt 打成 zip，方便用户整包发回来。"""
    import zipfile
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    detected = report.get('detected') or {}
    readme = [
        'Sai2-Sniffer 报告',
        '=================',
        '',
        f"生成时间：{report.get('generated_at')}",
        f"SAI2 路径：{(report.get('sai') or {}).get('exe_path')}",
        f"MD5：{(report.get('sai') or {}).get('exe_md5')}",
        f"版本：{((report.get('sai') or {}).get('version_info') or {}).get('FileDescription')}",
        '',
    ]
    if detected:
        readme += [
            f"探测结果：session_offset = {detected.get('session_offset')}",
            f"           布局 = {detected.get('layout')}  链长 = {detected.get('chain_len')}"
            f"  头节点 = {detected.get('head_name')!r}",
            '',
            f"画布缩略图：{(report.get('verified') or {}).get('png') or '无'}",
            '（缩略图能看出画面就是对的；花屏或全灰说明布局不匹配）',
        ]
    else:
        # 失败的时候最该写清楚原因 —— 用户只看这一个文件，不会去翻 report.json。
        # 以前这里只有一行「探测结果：{}」，等于什么都没说。
        readme.append('探测结果：失败')
        for err in report.get('errors') or ['没有更详细的信息']:
            readme.append(f'  · {err}')
        readme += ['', '候选列表在 report.json 的 candidates 里，一并发回来即可。']
        if report.get('node_dumps'):
            readme += [
                f"另外附了 {len(report['node_dumps'])} 个候选节点的内存片段（nodes.txt）。",
                '那是运行时的数据，不含你的软件本体 —— 作者靠它判断结构布局。',
            ]

    readme += [
        '',
        '把这个 zip 原样发回来即可，不要改动里面的文件。',
        '',
        '遇到问题或想反馈，加 QQ 群 942992768。',
    ]
    (out_dir / '说明.txt').write_text('\n'.join(readme), encoding='utf-8')

    # 候选节点的内存 dump 单独存一份纯文本 —— 作者不用解 json 就能直接看字节
    dumps = report.get('node_dumps') or []
    if dumps:
        blocks = ['候选节点内存 dump（运行时数据，不含软件本体）', '=' * 40, '']
        for d in dumps:
            blocks += [
                f"offset={d['offset']}  layout={d['layout']}  node={d['node']}",
                f"head={d['head_name']!r}  chain={d['chain_len']}  score={d['score']}",
                '',
                d['hex'],
                '',
            ]
        (out_dir / 'nodes.txt').write_text('\n'.join(blocks), encoding='utf-8')

    md5 = ((report.get('sai') or {}).get('exe_md5') or 'unknown')[:8]
    stamp = time.strftime('%Y%m%d-%H%M%S')
    zip_path = out_dir / f'Sai2-Sniffer-{md5}-{stamp}.zip'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name in ('report.json', '说明.txt', 'canvas.png', 'nodes.txt'):
            p = out_dir / name
            if p.exists():
                zf.write(p, name)
    return zip_path


# ---------------------------------------------------------------- 命令行
def cli_main(opts) -> int:
    try:                              # 打包版 stdout 走系统 codepage，中文会变乱码
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    out_dir = Path(opts.out) if opts.out else Path.cwd() / 'sniffer-report'
    out_dir.mkdir(parents=True, exist_ok=True)

    # 打包版是 windowed，没有控制台 —— 输出同时写一份文件，否则没法验收。
    log_path = out_dir / 'cli.log'
    lines: list[str] = []

    def emit(text=''):
        lines.append(text)
        try:
            print(text)
        except Exception:
            pass

    def flush():
        try:
            log_path.write_text('\n'.join(lines), encoding='utf-8')
        except OSError:
            pass

    report = scan(pid=opts.pid, log=emit, out_dir=out_dir)
    emit()
    if report.get('detected'):
        emit('=== 结果 ===')
        for k, v in report['detected'].items():
            emit(f'  {k:20s} {v}')
        v = report.get('verified') or {}
        for c in v.get('canvases', []):
            emit(f"  画布 {c.get('name')!r} {c.get('size')} "
                 f"level={c.get('level')} read={c.get('read')} "
                 f"shape={c.get('array_shape')}")
        emit()
        emit('=== 可粘贴的类 ===')
        emit(report['class_snippet'])
    else:
        emit('未探测成功：')
        for e in report.get('errors', []):
            emit('  - ' + str(e))
    zip_path = write_report(report, out_dir)
    emit(f'\n报告：{zip_path}')
    flush()
    return 0 if report.get('detected') else 2


# ---------------------------------------------------------------- 图形界面
def temp_dir() -> Path:
    """报告先落在临时目录。用户双击 exe 时 cwd 可能是 Program Files 之类只读位置，
    直接往 cwd 写会在保存那一刻才炸。"""
    base = os.environ.get('TEMP') or os.environ.get('TMP') or str(Path.home())
    path = Path(base) / 'Sai2-Sniffer'
    path.mkdir(parents=True, exist_ok=True)
    return path


def gui_main(autoscan: bool = False) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    BG, FG, SUB, LINE = '#f5f5f6', '#1c1f22', '#6b7075', '#dcdee1'
    ACCENT = '#3f6fb5'

    root = tk.Tk()
    root.title(f'Sai2-Sniffer v{SNIFFER_VERSION}')
    root.configure(bg=BG)
    root.geometry('760x665')
    root.minsize(680, 580)

    style = ttk.Style(root)
    try:
        style.theme_use('clam')
    except tk.TclError:
        pass
    style.configure('TFrame', background=BG)
    style.configure('TLabel', background=BG, foreground=FG, font=('Microsoft YaHei UI', 10))
    style.configure('Sub.TLabel', foreground=SUB, font=('Microsoft YaHei UI', 9))
    style.configure('Title.TLabel', font=('Microsoft YaHei UI', 12, 'bold'))
    style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=(12, 6))
    style.configure('Accent.TButton', font=('Microsoft YaHei UI', 10, 'bold'), padding=(16, 8))
    style.configure('TProgressbar', background=ACCENT)
    style.configure('Line.TFrame', background=LINE)

    state = {'pid': None, 'info': None, 'report': None, 'busy': False,
             'queue': queue.Queue(), 'zip': None}

    outer = ttk.Frame(root, padding=(18, 14, 18, 12))
    outer.pack(fill='both', expand=True)

    # --- 头部
    head = ttk.Frame(outer)
    head.pack(fill='x')
    ttk.Label(head, text='SAI2 版本嗅探器', style='Title.TLabel').pack(side='left')
    ttk.Label(head, text='  只读取，不修改任何东西', style='Sub.TLabel').pack(side='left', pady=(4, 0))

    info_box = tk.Frame(outer, bg='#ffffff', highlightbackground=LINE,
                        highlightthickness=1)
    info_box.pack(fill='x', pady=(10, 0))
    info_txt = tk.Text(info_box, height=5, bg='#ffffff', fg=FG, relief='flat',
                       font=('Consolas', 9), padx=10, pady=8, wrap='none')
    info_txt.pack(fill='x')
    info_txt.configure(state='disabled')

    def set_info(lines):
        info_txt.configure(state='normal')
        info_txt.delete('1.0', 'end')
        info_txt.insert('1.0', '\n'.join(lines))
        info_txt.configure(state='disabled')

    bar = ttk.Frame(outer)
    bar.pack(fill='x', pady=(12, 0))
    scan_btn = ttk.Button(bar, text='开始扫描', style='Accent.TButton')
    scan_btn.pack(side='left')
    redetect_btn = ttk.Button(bar, text='重新检测', command=lambda: detect(True))
    redetect_btn.pack(side='left', padx=(8, 0))
    prog = ttk.Progressbar(bar, mode='determinate', value=0, length=240)
    prog.pack(side='left', padx=(14, 0), fill='x', expand=True)

    # 底部固定区必须先 pack、且用 side='bottom'。日志区带 expand=True，
    # 如果它先 pack 就会吃掉全部剩余高度，foot 和下面那行提示被挤出窗口 ——
    # 而「保存报告」正是扫完唯一要点的按钮。
    # 2026-11-19 有人截图来问「为什么没看到导出按钮」，就是撞上这个：
    # 他的显示缩放不是 100%，760x665 的逻辑尺寸装不下全部内容。
    ttk.Label(
        outer,
        text='报告里会附一张画布缩略图 —— 那是判断参数对不对的唯一证据。'
             '把报告发给别人之前，留意一下画布内容。',
        style='Sub.TLabel',
    ).pack(side='bottom', fill='x', pady=(8, 0))

    foot = ttk.Frame(outer)
    foot.pack(side='bottom', fill='x', pady=(12, 0))
    save_btn = ttk.Button(foot, text='保存报告（zip）', state='disabled')
    save_btn.pack(side='left')
    copy_btn = ttk.Button(foot, text='复制类定义', state='disabled')
    copy_btn.pack(side='left', padx=(8, 0))
    hint = ttk.Label(foot, text='', style='Sub.TLabel')
    hint.pack(side='right')

    log_frame = tk.Frame(outer, bg='#ffffff', highlightbackground=LINE,
                         highlightthickness=1)
    log_frame.pack(fill='both', expand=True, pady=(12, 0))
    log_txt = tk.Text(log_frame, bg='#ffffff', fg=FG, relief='flat',
                      font=('Consolas', 9), padx=10, pady=8, wrap='word')
    scroll = ttk.Scrollbar(log_frame, command=log_txt.yview)
    log_txt.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    log_txt.pack(side='left', fill='both', expand=True)

    def log(text):
        log_txt.insert('end', text + '\n')
        log_txt.see('end')

    # --- 检测
    def detect(manual=False):
        if state['busy']:
            return
        if manual:
            log_txt.delete('1.0', 'end')
        pid = sai.find_running_sai_pid()
        if not pid:
            state['pid'] = None
            set_info(['未找到 SAI2 进程',
                      '',
                      '请先打开 SAI2（PaintTool SAI Ver.2），',
                      '并在里面打开至少一个画布，然后点「重新检测」。'])
            scan_btn.configure(state='disabled')
            return
        state['pid'] = pid
        info = collect_exe_info(pid)
        state['info'] = info
        vi = info.get('version_info') or {}
        mask = lambda s: f'{s[:8]}…{s[-4:]}' if s and len(s) > 12 else (s or '?')
        lines = [
            f"路径  {info.get('exe_path')}",
            f"版本  {vi.get('FileDescription') or vi.get('ProductVersion') or '读不到版本资源'}",
            f"MD5   {info.get('exe_md5')}   ({info.get('exe_size', 0)} 字节)",
            f"状态  {'✓ Sai2-Timelapse 版本表里已有这一版' if info.get('known_to_timelapse') else '✗ 版本表里没有这一版 —— 需要扫描出适配参数'}",
        ]
        set_info(lines)
        scan_btn.configure(state='normal' if not state['busy'] else 'disabled')

    # --- 扫描
    def do_scan():
        if state['busy'] or not state['pid']:
            return
        state['busy'] = True
        scan_btn.configure(state='disabled')
        redetect_btn.configure(state='disabled')
        save_btn.configure(state='disabled')
        copy_btn.configure(state='disabled')
        hint.configure(text='')
        prog.configure(mode='indeterminate')
        prog.start(12)
        log_txt.delete('1.0', 'end')

        def work():
            try:
                report = scan(pid=state['pid'],
                              log=lambda s: state['queue'].put(('log', s)),
                              out_dir=temp_dir())
                state['queue'].put(('done', report))
            except Exception:
                state['queue'].put(('fatal', traceback.format_exc()))

        threading.Thread(target=work, daemon=True).start()

    def poll():
        try:
            while True:
                kind, payload = state['queue'].get_nowait()
                if kind == 'log':
                    log(payload)
                elif kind == 'done':
                    finish(payload)
                elif kind == 'fatal':
                    log(payload)
                    finish(None)
        except queue.Empty:
            pass
        root.after(80, poll)

    def finish(report):
        state['busy'] = False
        prog.stop()
        prog.configure(mode='determinate', value=0)
        redetect_btn.configure(state='normal')
        scan_btn.configure(state='normal')
        state['report'] = report
        # 只要扫过一轮就有报告可存 —— 哪怕验证全部失败。失败报告里带着候选列表
        # 和节点字节 dump，那是判断「这个版本还能不能救」的唯一依据。
        # 以前只有成功才启用，结果扫失败的人反过来问「保存按钮是灰的，我怎么发给你」。
        if report:
            save_btn.configure(state='normal')
        if report and report.get('detected'):
            copy_btn.configure(state='normal')
            d = report['detected']
            v = report.get('verified') or {}
            log('')
            log(f"✓ 适配参数：session_offset = {d['session_offset']}  布局 {d['layout']}")
            for c in v.get('canvases', []):
                log(f"  画布 {c.get('name')!r}  {c.get('size')}  读图={c.get('read')}  "
                    f"{c.get('array_shape')}")
            hint.configure(text='下一步：点「保存报告」，把 zip 发回来')
        else:
            hint.configure(
                text='扫描没成功 —— 也点「保存报告」，里面带着诊断数据' if report
                else '扫描没成功 —— 日志里有原因')

    def save_report():
        report = state['report']
        if not report:
            return
        default = f"Sai2-Sniffer-{(report.get('sai') or {}).get('exe_md5', 'unknown')[:8]}"
        path = filedialog.asksaveasfilename(
            title='保存报告', defaultextension='.zip',
            initialfile=default + '.zip',
            filetypes=[('Zip 包', '*.zip')])
        if not path:
            return
        try:
            tmp = temp_dir()
            report['saved_copy'] = path
            write_report(report, tmp)
            import shutil
            zips = sorted(tmp.glob('Sai2-Sniffer-*.zip'), key=lambda p: p.stat().st_mtime)
            shutil.copyfile(zips[-1], path)
            state['zip'] = path
            log(f'报告已保存：{path}')
            messagebox.showinfo('已保存', f'报告已保存到：\n{path}\n\n把这个 zip 文件发回来即可。')
        except Exception as exc:
            messagebox.showerror('保存失败', str(exc))

    def copy_snippet():
        report = state['report'] or {}
        snippet = report.get('class_snippet')
        if not snippet:
            return
        root.clipboard_clear()
        root.clipboard_append(snippet)
        hint.configure(text='类定义已复制')

    scan_btn.configure(command=do_scan)
    save_btn.configure(command=save_report)
    copy_btn.configure(command=copy_snippet)

    detect()
    root.after(80, poll)
    if autoscan:                      # 自检钩子：启动即扫，方便截图核对界面状态
        root.after(300, do_scan)
    root.mainloop()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='Sai2-Sniffer — 读取 SAI2 适配参数')
    ap.add_argument('--cli', action='store_true', help='命令行模式（不出界面）')
    ap.add_argument('--pid', type=int, help='指定 SAI2 进程号')
    ap.add_argument('--out', help='报告输出目录')
    ap.add_argument('--autoscan', action='store_true',
                    help='界面启动后立刻扫描一次（自检用）')
    opts = ap.parse_args()
    if opts.cli:
        return cli_main(opts)
    return gui_main(autoscan=opts.autoscan)


if __name__ == '__main__':
    sys.exit(main())
