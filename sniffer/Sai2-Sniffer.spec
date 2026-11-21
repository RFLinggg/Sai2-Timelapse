# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Sai2-Sniffer.

Build from the project root:
    venv\\Scripts\\python.exe -m PyInstaller sniffer\\Sai2-Sniffer.spec --noconfirm

输出 dist/Sai2-Sniffer/ （文件夹版）。故意不做单文件：
单文件每次启动要解压到 %TEMP%，冷启动 4~9 秒，而且
「读取其他进程内存 + 无签名 + 自解压」这个特征组合在 Windows Defender 眼里
和外挂一模一样，误报概率高得多。
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve()                 # sniffer/（SPECPATH 就是 spec 所在目录）
PROJ = ROOT.parent                              # art-timelapse/
SRC = PROJ / 'src'

# ---------------------------------------------------------------- data files
datas = []

# 检查工具有自己的图标，不跟主程序走。主程序 2026-11-18 换过一次图标，
# 而这里的 spec 一直引用同一个 Sai2-Timelapse.ico —— 只要重打包嗅探器，
# 它的图标就会跟着变。用户明确说过不要动检查工具的图标。
ico = ROOT / 'Sai2-Sniffer.ico'
if not ico.exists():
    ico = PROJ / 'Sai2-Timelapse.ico'     # 老目录还没放独立图标的兜底

# ------------------------------------------------------------ hidden imports
# PyMemoryEditor 按平台挑后端模块，PyInstaller 的静态分析看不到。
hiddenimports = collect_submodules('PyMemoryEditor')
hiddenimports += ['psutil']

a = Analysis(
    [str(ROOT / 'sniffer.py')],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 界面用 tkinter，其余重货全部排除 —— 这个工具只需要读内存 + 写 PNG。
    excludes=[
        'cv2', 'webview', 'pythonnet', 'clr', 'ttkbootstrap',
        'matplotlib', 'scipy', 'pandas', 'IPython', 'notebook', 'jupyter',
        'pytest', 'setuptools', 'pip', 'test', 'unittest',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'art_timelapse.gui', 'art_timelapse.webapp', 'art_timelapse.timelapse',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Sai2-Sniffer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ico) if ico.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Sai2-Sniffer',
)
