"""为 Sai2-Timelapse 创建 Windows 快捷方式（桌面 + 开始菜单）。

用 pywin32 走进程内 COM（PowerShell 的 COM 实例化被安全策略拦截，cscript 也被拦）。

两种目标：
  源码版（默认）  直接指向 venv 里的 pythonw.exe，零黑框闪窗
  独立版（--exe） 指向 dist/Sai2-Timelapse.exe（PyInstaller 打包的单文件）

用法：
    python tools/make_shortcuts.py            # 桌面 + 开始菜单（源码版）
    python tools/make_shortcuts.py --exe      # 桌面 + 开始菜单（独立 exe）
    python tools/make_shortcuts.py --desktop  # 只建桌面
    python tools/make_shortcuts.py --list     # 只看会建哪些，不落盘
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import win32com.client

ROOT = Path(__file__).resolve().parent.parent
PYTHONW = ROOT / 'venv' / 'Scripts' / 'pythonw.exe'
PYTHON = ROOT / 'venv' / 'Scripts' / 'python.exe'
ICON = ROOT / 'Sai2-Timelapse.ico'
BAT_NORMAL = ROOT / '启动 Sai2-Timelapse.bat'
BAT_DEBUG = ROOT / '启动（带控制台-排错用）.bat'
EXE_ONEFILE = ROOT / 'dist' / 'Sai2-Timelapse.exe'
EXE_ONEDIR = ROOT / 'dist' / 'Sai2-Timelapse' / 'Sai2-Timelapse.exe'

# 名字里不能带 🔔：快捷方式走的是 ANSI 代码页（GBK 编不出这个字符，
# 会变成 '?'，而 '?' 是非法文件名字符），Save 会直接失败。
NAME_MAIN = 'Sai2-Timelapse'
NAME_DEBUG = 'Sai2-Timelapse（排错）'
DESC = 'Sai2-Timelapse — SAI2 绘画过程缩时录制（直接读画布内存，非录屏）'


def preflight(use_exe: bool, onefile: bool = False) -> list[str]:
    problems = []
    if use_exe:
        required = (ICON, EXE_ONEFILE if onefile else EXE_ONEDIR)
    else:
        required = (PYTHONW, PYTHON, ICON, BAT_NORMAL, BAT_DEBUG)
    for p in required:
        if not p.exists():
            problems.append(f'缺失: {p}')
    return problems


def make(shell, lnk_path: Path, target: Path, args: str, icon: Path,
         desc: str, workdir: Path = ROOT) -> None:
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    sc = shell.CreateShortcut(str(lnk_path))
    sc.TargetPath = str(target)
    sc.Arguments = args
    sc.WorkingDirectory = str(workdir)
    sc.IconLocation = f'{icon},0'
    sc.Description = desc
    sc.WindowStyle = 1  # SW_SHOWNORMAL
    sc.Save()


def verify(shell, lnk_path: Path) -> tuple[str, str]:
    sc = shell.CreateShortcut(str(lnk_path))
    return sc.TargetPath, sc.Arguments


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--desktop', action='store_true', help='只建桌面快捷方式')
    ap.add_argument('--exe', action='store_true',
                    help='指向打包版（默认文件夹版，启动约 1 秒）')
    ap.add_argument('--onefile', action='store_true',
                    help='配合 --exe：改指向单文件版（启动约 3 秒）')
    ap.add_argument('--list', action='store_true', help='只打印计划，不落盘')
    opts = ap.parse_args()

    problems = preflight(opts.exe, opts.onefile)
    if problems:
        print('前置检查失败：')
        for p in problems:
            print('  -', p)
        return 1

    shell = win32com.client.Dispatch('WScript.Shell')
    desktop = Path(shell.SpecialFolders('Desktop'))
    programs = Path(shell.SpecialFolders('Programs'))

    if opts.exe:
        if opts.onefile:
            target, note = EXE_ONEFILE, '（单文件版，启动约 3 秒）'
        else:
            target, note = EXE_ONEDIR, '（文件夹版，启动约 1 秒）'
        args = ''
    else:
        target, args, note = PYTHONW, '-m art_timelapse', '（源码版，无控制台）'

    plan = [(desktop / f'{NAME_MAIN}.lnk', target, args, f'{DESC}{note}')]
    if not opts.desktop:
        plan.append((programs / f'{NAME_MAIN}.lnk', target, args, f'{DESC}{note}'))
        # 排错入口始终指向源码版：exe 是 windowed，出错看不到堆栈。
        if BAT_DEBUG.exists():
            plan.append((programs / f'{NAME_DEBUG}.lnk', BAT_DEBUG, '',
                         'Sai2-Timelapse 排错启动：出错时显示控制台与堆栈'))

    print(f'项目根   : {ROOT}')
    print(f'桌面     : {desktop}')
    print(f'开始菜单 : {programs}')
    print(f'目标     : {"独立 exe" if opts.exe else "源码（pythonw）"}')
    print()
    print('计划创建：')
    for path, tgt, a, _ in plan:
        print(f'  {path}')
        print(f'      -> {tgt} {a}'.rstrip())
    print()

    if opts.list:
        print('(--list 模式，未落盘)')
        return 0

    ok = 0
    for path, tgt, a, desc in plan:
        try:
            make(shell, path, tgt, a, ICON, desc)
            t, a2 = verify(shell, path)
            status = 'OK' if Path(t) == tgt else 'WARN(读回不一致)'
            print(f'[{status}] {path.name}  ->  {t} {a2}'.rstrip())
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f'[FAIL] {path}: {e}')

    print()
    print(f'完成 {ok}/{len(plan)}')
    return 0 if ok == len(plan) else 2


if __name__ == '__main__':
    sys.exit(main())
