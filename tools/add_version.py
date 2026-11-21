"""把一份嗅探报告变成版本表条目 —— 减少手抄 MD5 / offset 抄错的机会。

    # 只看：打印将写入的类 + 生成外部版本表 JSON（不动源码）
    venv\\Scripts\\python.exe tools\\add_version.py --report "D:\\...\\Sai2-Sniffer-xxxxxxxx.zip"

    # 真写：插进 src/art_timelapse/sai.py，并把版本号抬一位
    venv\\Scripts\\python.exe tools\\add_version.py --report "..." --apply

    # 版本号先不动（补一个已发版本漏掉的条目时用）
    venv\\Scripts\\python.exe tools\\add_version.py --report "..." --apply --no-bump

它不做判断。报告结构对不等于参数对 —— 图还是得自己看（见交接说明.md 3.2）。
"""
import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

SAI_PY = ROOT / 'src' / 'art_timelapse' / 'sai.py'
INIT_PY = ROOT / 'src' / 'art_timelapse' / '__init__.py'
VERINFO = ROOT / 'packaging' / 'version_info.txt'
README = ROOT / 'packaging' / '使用说明.txt'
DIST = ROOT / 'dist'

# 新类插在这里之前 —— 它在所有版本类之后、工具函数之前，属同一区块。
ANCHOR = '\ndef _find_pid_by_exe_names_win32('


def read_report(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if 'report.json' not in names:
            raise SystemExit('不是有效报告：缺 report.json')
        if 'nodes.txt' in names:
            raise SystemExit('这是失败报告（带 nodes.txt），没有验证结果，不能直接加')
        report = json.loads(z.read('report.json'))
    canvases = (report.get('verified') or {}).get('canvases') or []
    if not canvases:
        raise SystemExit('报告里没有任何实读验证，无法判断参数对不对')
    return report


def pick_fields(report):
    sai = report.get('sai') or {}
    det = report.get('detected') or {}
    md5 = sai.get('exe_md5') or ''
    if not re.fullmatch(r'[0-9a-fA-F]{32}', md5):
        raise SystemExit(f'MD5 字段不可用：{md5!r}')

    desc = (sai.get('version_info') or {}).get('FileDescription') or ''
    # 表里一律用 'SAI Ver.2 (64bit) ...'，把厂商前缀去掉，免得同一个版本两种写法
    name = re.sub(r'^PaintTool\s+', '', desc.strip())
    prod = (sai.get('version_info') or {}).get('ProductVersion') or ''
    if not name:
        raise SystemExit('版本资源里没有 FileDescription，只能手动写版本名')

    off = det.get('session_offset_int')
    if off is None:
        raise SystemExit('报告里没有 detected.session_offset_int')
    return {
        'md5': md5.lower(),
        'name': name,
        'product': prod,
        'offset': int(off),
        'layout': det.get('layout') or 'base',
        'head': det.get('head_name') or '',
        'canvases': (report.get('verified') or {}).get('canvases') or [],
        'snippet': report.get('class_snippet') or '',
        'exe_size': sai.get('exe_size'),
        'exe_path': sai.get('exe_path'),
        'layout_label': det.get('layout_label') or '',
    }


def canvas_summary(canvases):
    """挑一条最像「有画面」的写进注释，没有就退到第一条。"""
    best = max(canvases, key=lambda c: c.get('std') or 0)
    size = best.get('size') or [0, 0]
    shape = best.get('array_shape') or [0, 0]
    return (f"{best.get('name')!r} {size[0]}x{size[1]} -> {shape[1]}x{shape[0]}, "
            f"mean {best.get('mean')} / std {best.get('std')}")


def class_name_for(spec, existing_text):
    base = 'SAIv2_API_' + re.sub(r'\D', '_', spec['product'] or 'unknown')
    base = re.sub(r'_+', '_', base).strip('_')
    if f'class {base}(' not in existing_text:
        return base
    for suffix in 'bcdefg':
        cand = f'{base}_{suffix}'
        if f'class {cand}(' not in existing_text:
            return cand
    raise SystemExit('同名类太多了，手工起个名字')


def build_block(spec, cls_name):
    head_note = f" head node {spec['head']!r}." if spec['head'] else ''
    off_user = (spec['offset'] & 0xF) != 0
    warn = ('\n    # offset is NOT 16-byte aligned — double-check this came from the right\n'
            '    # candidate before trusting it.\n') if off_user else ''
    return f'''@register_sai_api
class {cls_name}(SAIv2_API_Base):
    # {spec['name']} ({spec['exe_path']}, {spec['exe_size']} bytes), base layout.{head_note}
    # Verified with content: {canvas_summary(spec['canvases'])}
    # If that std is low, it means a pale lineart on white — check the thumbnail
    # before deciding the read was wrong. Blank canvases have std < 2.
    version_name = '{spec['name']}'
    exe_hash = '{spec['md5']}'
    session_offset = {hex(spec['offset'])}
''' + warn


def plan_bump():
    """算出版本号要写的三个文件，先算后写 —— 中途报错不至于留下半改的状态。

    版本号有两套写法，别混：源码里是 '1.25'，版本资源里是 (1, 2, 5, 0)，
    后者的第 2、3 段是 minor 的十位和个位，所以 1.29 之后是 (1, 3, 0, 0)。
    """
    src = INIT_PY.read_text(encoding='utf-8')
    m = re.search(r"__version__ = '(\d+)\.(\d+)'", src)
    if not m:
        raise SystemExit('找不到 __version__')
    major, minor = int(m.group(1)), int(m.group(2))
    old, new = f'{major}.{minor}', f'{major}.{minor + 1}'
    n_major, n_minor = major, minor + 1

    vu = VERINFO.read_text(encoding='utf-8')
    pairs = [
        (f'({major}, {minor // 10}, {minor % 10}, 0)',
         f'({n_major}, {n_minor // 10}, {n_minor % 10}, 0)'),
        (f"'FileVersion', '{old}'", f"'FileVersion', '{new}'"),
        (f"'ProductVersion', '{old}'", f"'ProductVersion', '{new}'"),
    ]
    for a, b in pairs:
        if a not in vu:
            raise SystemExit(f'version_info.txt 里找不到 {a!r}，三处版本号可能已经不同步')
        vu = vu.replace(a, b)

    rd = README.read_text(encoding='utf-8')
    if f'v{old}（Windows 64 位）' in rd:
        rd = rd.replace(f'v{old}（Windows 64 位）', f'v{new}（Windows 64 位）', 1)

    return (old, new), [
        (INIT_PY, src.replace(f"__version__ = '{old}'", f"__version__ = '{new}'")),
        (VERINFO, vu),
        (README, rd),
    ]


def write_external(spec):
    spec_json = {
        'schema': 1,
        '_comment': (f"{spec['name']} ({spec['md5'][:8]})。放到 Sai2-Timelapse.exe 旁边，"
                     f"或 C:\\Users\\<你>\\.art-timelapse-versions.json，重开程序生效。"),
        'versions': [{
            'hash': spec['md5'],
            'name': spec['name'],
            'offset': hex(spec['offset']),
            'layout': spec['layout'],
        }],
    }
    out = DIST / f"sai-versions.{spec['md5'][:8]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec_json, ensure_ascii=False, indent=2), encoding='utf-8')
    return out


def main():
    ap = argparse.ArgumentParser(description='把嗅探报告变成版本表条目')
    ap.add_argument('--report', required=True, help='Sai2-Sniffer 导出的 zip')
    ap.add_argument('--apply', action='store_true', help='真写进 sai.py 并抬版本号')
    ap.add_argument('--no-bump', action='store_true', help='不抬版本号')
    ap.add_argument('--note', default='', help='额外一行注释')
    args = ap.parse_args()

    report = read_report(Path(args.report))
    spec = pick_fields(report)

    from art_timelapse import sai
    if spec['md5'] in sai.sai_api_lookup:
        known = sai.sai_api_lookup[spec['md5']]
        print(f"这个 MD5 已经在版本表里：{known.__name__}  "
              f"{known.version_name}  {hex(known.session_offset)}")
        print('不用重复加。如果用户还是抓不到画布，去查别的（编码器 / 路径 / 权限）。')
        return 0

    text = SAI_PY.read_text(encoding='utf-8')
    cls_name = class_name_for(spec, text)
    block = build_block(spec, cls_name)
    if args.note:
        block = block.replace('# Verified with content:', f"# {args.note}\n    # Verified with content:")

    ext = write_external(spec)
    print(f"外部版本表：{ext}（{ext.stat().st_size} 字节）")
    print(f"MD5 {spec['md5']}  offset {hex(spec['offset'])}  layout {spec['layout']}")
    print('\n' + '-' * 60)
    print(block)

    if not args.apply:
        print('-' * 60)
        print('上面这段是预览。加 --apply 才会写进 sai.py 并抬版本号。')
        return 0

    if spec['layout'] != 'base':
        if '_fields_' not in spec['snippet']:
            raise SystemExit('new 布局必须带 SAICanvas._fields_ 覆盖，报告片段里没有，'
                             '手工从 report.json 的 class_snippet 抄全再插。')
        block = ('# new layout: field table differs from SAIv2_API_Base, see _fields_ below.\n'
                 + spec['snippet'].strip() + '\n\n')

    if ANCHOR not in text:
        raise SystemExit('sai.py 里找不到插入锚点，文件可能被改过，手工插。')

    # 先把版本号那边算完再落盘：算不出来就什么都不写，别留下「类进去了、版本没抬」。
    bumps = []
    ver_pair = None
    if not args.no_bump:
        ver_pair, bumps = plan_bump()

    text = text.replace(ANCHOR, '\n' + block + ANCHOR, 1)
    SAI_PY.write_text(text, encoding='utf-8')
    print(f'已写入 {SAI_PY}')

    for path, content in bumps:
        path.write_text(content, encoding='utf-8')
    if ver_pair:
        print(f'版本号 {ver_pair[0]} -> {ver_pair[1]}（__init__.py / version_info.txt / 使用说明.txt）')
    print('\n下一步：')
    print('  1. 编译：venv\\Scripts\\python.exe -m py_compile src\\art_timelapse\\sai.py')
    print('  2. 确认：venv\\Scripts\\python.exe -c "import sys;sys.path.insert(0,\'src\');'
          'from art_timelapse import sai;print(len(sai.sai_api_lookup))"')
    print('  3. 构建 + 打包，见 交接说明.md 第 5 节')
    return 0


if __name__ == '__main__':
    sys.exit(main())
