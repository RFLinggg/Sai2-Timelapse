"""一条命令看完嗅探报告，并给出「能不能加进版本表」的初判。

用法（项目根目录下）：

    venv\\Scripts\\python.exe tools\\inspect_report.py "D:\\path\\to\\Sai2-Sniffer-xxxxxxxx.zip"

它做四件事：
  1. 打印说明.txt / exe 信息 / 候选表 / 验证表
  2. 把 canvas.png 存到 probe/incoming/<hash>_canvas.png（后面要用肉眼看）
  3. 自己算缩放比例（两轴必须一致）、统计量、占位头节点
  4. 输出一行「初判」，把需要人判断的点标出来 —— 它不替你看图，std 低不代表空白

退出码：0 = 结构上通过；1 = 报告不可用（失败报告或验证全灭）。
初判通过不等于可以直接发版，图必须看。
"""
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / 'probe' / 'incoming'

# 占位画布的叫法：日文版是「便签本」，其余是「便签薄」。只作参考，不参与判定。
PLACEHOLDER_HINTS = ('便签薄', '便签本')


def load(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        report = json.loads(z.read('report.json')) if 'report.json' in names else None
        note = z.read('说明.txt').decode('utf-8-sig') if '说明.txt' in names else ''
        png = z.read('canvas.png') if 'canvas.png' in names else None
        nodes = z.read('nodes.txt').decode('utf-8', 'replace') if 'nodes.txt' in names else None
    return names, report, note, png, nodes


def ratio_verdict(size, shape):
    """把 原尺寸 / 读出尺寸 折算成整数倍，两轴必须一致且在 1/2/4/8 附近。"""
    if not size or not shape or len(size) != 2 or len(shape) < 2:
        return False, '尺寸字段缺失'
    w, h = int(size[0]), int(size[1])
    rh, rw = int(shape[0]), int(shape[1])       # numpy 是 (h, w, c)
    if min(w, h, rw, rh) <= 0:
        return False, f'尺寸非法 {size} / {shape}'
    kx, ky = w / rw, h / rh
    if abs(kx - ky) > 0.02:
        return False, f'两轴比例不一致 x={kx:.2f} y={ky:.2f}'
    if any(abs(kx - k) < 0.02 for k in (1, 2, 4, 8)):
        return True, f'缩放 {kx:.0f}:1（{w}x{h} -> {rw}x{rh}）'
    return False, f'比例 {kx:.2f} 不在 1/2/4/8 之列'


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    path = Path(argv[1])
    if not path.exists():
        print(f'找不到文件：{path}')
        return 1

    names, report, note, png, nodes = load(path)
    print(f'文件：{path.name}（{path.stat().st_size / 1024:.0f} KB）')
    print(f'内容：{names}')
    if nodes is not None:
        print('\n!! 带 nodes.txt —— 这是「扫描失败报告」，没有验证结果')
        print(nodes[:2000])
        return 1
    if report is None:
        print('!! 不是有效报告：没有 report.json')
        return 1

    print('\n' + '=' * 60)
    print(note.strip())
    print('=' * 60)

    sai = report.get('sai') or {}
    det = report.get('detected') or {}
    md5 = sai.get('exe_md5') or ''
    print(f"\n嗅探器版本：{report.get('sniffer_version')}")
    print(f"exe        ：{sai.get('exe_path')}")
    print(f"大小/时间  ：{sai.get('exe_size')} / {sai.get('exe_modified')}")
    print(f"MD5        ：{md5}")
    known = sai.get('known_to_timelapse')
    print(f"主程序认不认：{'认识 -> ' + str(sai.get('known_as')) if known else '不认识（需要加条目）'}")
    print(f"报告自述   ：offset={det.get('session_offset')} layout={det.get('layout')} "
          f"({det.get('layout_label')}, 方法={det.get('method')})")

    print('\n--- 候选（按分数降序，只看前几个）---')
    for i, c in enumerate((report.get('candidates') or [])[:8], 1):
        head = (c.get('head_name') or '')[:30]
        print(f"{i}. off={c.get('session_offset')} {c.get('layout')} "
              f"chain={c.get('chain_len')} score={c.get('score')} head={head!r}")

    canvases = (report.get('verified') or {}).get('canvases') or []
    print('\n--- 实读验证 ---')
    ok_all = True
    for c in canvases:
        good, why = (False, c.get('error') or '读取失败') if not c.get('read') \
            else ratio_verdict(c.get('size'), c.get('array_shape'))
        ok_all = ok_all and good
        print(f"  {c.get('name')!r}  size={c.get('size')} shape={c.get('array_shape')}")
        print(f"     mean={c.get('mean')} std={c.get('std')}  -> {'OK' if good else 'NG'} {why}")
        if c.get('error'):
            print(f"     error: {c['error']}")

    if png:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / f'{md5[:8] or "report"}_canvas.png'
        out.write_bytes(png)
        print(f'\n缩略图已存：{out}')
        print('  用看图工具打开它 —— 线条/颜色正常 = 参数对；花屏、错位、色道反 = 布局不符。')

    snippet = report.get('class_snippet')
    if snippet:
        print('\n--- 报告自带的类片段（可直接粘进 sai.py，但要按 3.3 的规矩补注释）---')
        print(snippet)

    heads = [c.get('head_name') or '' for c in (report.get('candidates') or [])[:1]]
    placeholder = any(h.startswith(hint) for h in heads for hint in PLACEHOLDER_HINTS)

    print('\n' + '-' * 60)
    print('初判：', '结构通过，待看图确认' if ok_all else '结构未通过，见上面 NG 行')
    print(f"  占位头节点：{'是' if placeholder else '否（第一个候选不是便签薄/便签本，留意）'}")
    print('  下一步：venv\\Scripts\\python.exe tools\\add_version.py --help')
    return 0 if ok_all else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
