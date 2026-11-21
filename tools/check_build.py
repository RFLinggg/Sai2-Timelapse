"""发版前的自检：把 dist 里的程序真正启动一次，核对标题和版本表条目数。

    venv\\Scripts\\python.exe tools\\check_build.py

为什么要真启动：窗口标题和日志里的「支持的 SAI 版本：N 个」是两处最容易忘改的地方 ——
版本号抬了、zip 名对了，标题还挂着旧号；或者新类根本没被 import 进来，条目数没涨。
这两件事不启动程序看不出来。

退出码 0 = 标题和条目数都对；1 = 有一项不符（会打印期望值和实际值）。
用户配置 ~/.art-timelapse 会先备份再还原，跑完不影响本机设置。
"""
import ctypes
import os
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

from art_timelapse import __version__, sai  # noqa: E402

EXE = ROOT / 'dist' / 'Sai2-Timelapse' / 'Sai2-Timelapse.exe'
CFG = Path.home() / '.art-timelapse'
BAK = Path.home() / '.art-timelapse.checkbak'
LOG = ROOT / 'probe' / 'check_build.log'


def find_title(timeout=20.0):
    u = ctypes.windll.user32
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    found = []

    def cb(hwnd, _):
        if u.IsWindowVisible(hwnd) and u.GetWindowTextLengthW(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(hwnd, buf, 256)
            if '🔔' in buf.value:
                found.append(buf.value)
        return True

    deadline = time.time() + timeout
    while time.time() < deadline and not found:
        time.sleep(0.5)
        found.clear()
        u.EnumWindows(proto(cb), 0)
    return found[0] if found else ''


def main():
    if not EXE.exists():
        print(f'找不到 {EXE} —— 先跑构建，见 交接说明.md 第 5 节')
        return 1

    if CFG.exists():
        shutil.copyfile(CFG, BAK)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.unlink(missing_ok=True)
    env = dict(os.environ)
    env['ART_TIMELAPSE_LOG'] = str(LOG.resolve())

    proc = subprocess.Popen([str(EXE)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        title = find_title()
        time.sleep(1.5)                      # 等日志把版本表那行刷出来
    finally:
        subprocess.run(['taskkill', '/F', '/PID', str(proc.pid)], capture_output=True)
        time.sleep(0.8)
        if BAK.exists():
            shutil.copyfile(BAK, CFG)
            BAK.unlink()

    want_ver = f'v{__version__}'
    want_n = len(sai.sai_api_lookup)
    counted = None
    if LOG.exists():
        for line in LOG.read_text(encoding='utf-8', errors='replace').splitlines():
            if '支持的 SAI 版本' in line:
                digits = ''.join(ch for ch in line.split('：')[-1] if ch.isdigit())
                counted = int(digits) if digits else None

    ok = True
    print(f'标题     ：{title or "(没抓到)"}')
    if want_ver not in title:
        print(f'  NG 期望包含 {want_ver}')
        ok = False
    print(f'版本表   ：{counted if counted is not None else "(日志里没有)"}')
    if counted != want_n:
        print(f'  NG 期望 {want_n}（sai_api_lookup 的长度）')
        ok = False

    # 只比标题和 __version__ 会漏掉一种事故：__version__ 忘了抬，而 version_info.txt
    # 和 zip 名抬了 —— 两边都以 __version__ 为准，标题当然"一致"，一路绿灯发出去，
    # 用户拿到叫 v1.26.1 的包、窗口写着 v1.25。所以再把说明书标题拉进来三方对齐。
    readme_ver = ''
    readme = ROOT / 'packaging' / '使用说明.txt'
    if readme.exists():
        first = readme.read_text(encoding='utf-8-sig').splitlines()[0] if readme.stat().st_size else ''
        if 'v' in first:
            readme_ver = 'v' + first.split('v', 1)[1].split('（')[0].strip()
    print(f'说明书   ：{readme_ver or "(读不到)"}')
    if readme_ver != want_ver:
        print(f'  NG 说明书标题和 __version__ 不一致（{readme_ver} vs {want_ver}）'
              f' —— 通常是某一处忘了改')
        ok = False

    # 第三处：PE 版本资源。Explorer 属性页读的是它，exe 文件名/zip 名也是它带出来的。
    pe_ver = ''
    vfile = ROOT / 'packaging' / 'version_info.txt'
    if vfile.exists():
        m = re.search(r"'FileVersion',\s*'([^']+)'", vfile.read_text(encoding='utf-8'))
        pe_ver = 'v' + m.group(1) if m else ''
    print(f'版本资源 ：{pe_ver or "(读不到)"}')
    if pe_ver != want_ver:
        print(f'  NG version_info.txt 的 FileVersion 和 __version__ 不一致'
              f'（{pe_ver} vs {want_ver}）')
        ok = False

    print('结果     ：' + ('通过' if ok else '不通过'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
