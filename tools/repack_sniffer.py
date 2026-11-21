"""重打 Sai2-Sniffer 发布包。

版本号从 sniffer/sniffer.py 的 SNIFFER_VERSION 读，zip 名和顶层目录都跟着走。
以前这是内联命令手写的，文件名一直挂着 v1.0，而里面的 exe 早就是 1.01 ——
用户看文件名会以为是老版本，其实不是。
"""
import re
import zipfile
from pathlib import Path

ROOT = Path('D:/Codex work/art-timelapse')
SNIFFER = ROOT / 'sniffer' / 'sniffer.py'
SRC = ROOT / 'dist' / 'Sai2-Sniffer'
README = ROOT / 'sniffer' / '使用说明.txt'

ver = re.search(r"^SNIFFER_VERSION\s*=\s*'([^']+)'",
                SNIFFER.read_text(encoding='utf-8'), re.M).group(1)
TOP = f'Sai2-Sniffer_v{ver}_win64'
ZP = ROOT / 'dist' / f'{TOP}.zip'

readme = README.read_text(encoding='utf-8')
n = 0
tmp = ROOT / 'dist' / '_sn.zip'
with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    z.writestr(f'{TOP}/使用说明.txt', ('\ufeff' + readme).encode('utf-8'))
    for p in sorted(SRC.rglob('*')):
        if p.is_file():
            z.write(p, f'{TOP}/{p.relative_to(SRC).as_posix()}')
            n += 1
tmp.replace(ZP)
print(f'{TOP}: {n} files + 1 readme, {ZP.stat().st_size / 1024 / 1024:.1f} MB')

with zipfile.ZipFile(ZP) as z:
    names = z.namelist()
    print('entries:', len(names))
    print('套层检查:', not any(x.count(TOP) > 1 for x in names))
    print('说明书含群号:', '942992768' in z.read(f'{TOP}/使用说明.txt').decode('utf-8-sig'))
