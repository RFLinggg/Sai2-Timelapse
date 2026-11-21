# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Sai2-Timelapse.

Build (from the project root):
    venv\\Scripts\\pyinstaller.exe packaging\\Sai2-Timelapse.spec --noconfirm

Two variants are produced by flipping ONEFILE below:
  ONEFILE = True   -> dist/Sai2-Timelapse.exe        (single file, slow start)
  ONEFILE = False  -> dist/Sai2-Timelapse/           (folder, fast start)
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ONEFILE=1 (default) -> dist/Sai2-Timelapse.exe       single file, ~5s cold start
# ONEFILE=0           -> dist/Sai2-Timelapse/           folder, ~1s cold start
# Override without editing this file:  set ONEFILE=0 && pyinstaller ...
ONEFILE = os.environ.get('ONEFILE', '1') != '0'

# SPECPATH is the directory containing this spec file (i.e. <root>/packaging).
ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / 'src'
PKG = SRC / 'art_timelapse'

# ---------------------------------------------------------------- data files
datas = []

# Compiled gettext catalogs -> art_timelapse/locales/<lang>/LC_MESSAGES/*.mo
locales_root = PKG / 'locales'
for mo in sorted(locales_root.rglob('*.mo')):
    rel_parent = mo.relative_to(locales_root).parent
    datas.append((str(mo), str(Path('art_timelapse') / 'locales' / rel_parent)))

# Window icon (also embedded into the exe itself via the `icon` option below)
ico = ROOT / 'Sai2-Timelapse.ico'
if ico.exists():
    datas.append((str(ico), '.'))

# WebView 界面的静态资源（index.html / style.css / app.js）。
# 这三个文件是运行时用 open() 读的，PyInstaller 的依赖分析看不到，必须手动收。
webui_root = PKG / 'webui'
for asset in sorted(webui_root.glob('*')):
    if asset.is_file():
        datas.append((str(asset), str(Path('art_timelapse') / 'webui')))

# OpenH264 —— OpenCV 的 avc1（H.264）编码器在运行时 dlopen 这个库。
# 不带它的话，avc1 在别人机器上直接 isOpened() = False（2026-11-19 那台 ASUS
# 上就是这样，用户只看到「当前版本不支持编码器 avc1」然后录制被停掉）；
# 在这台机器上更阴险 —— isOpened() 返回 True，但只写出 1085 字节的空壳。
# cv2/__init__.py 会把包目录 add_dll_directory 进去，所以放到 cv2/ 子目录即可。
openh264 = ROOT / 'packaging' / 'vendor' / 'openh264-2.5.0-win64.dll'
if openh264.exists():
    datas.append((str(openh264), 'cv2'))
else:
    print('WARNING: openh264 dll 缺失，avc1 将不可用')

# ttkbootstrap ships theme json + image assets that are loaded at runtime.
# 只在退回 Tk 界面时才用得上，但回退路径必须能跑，不能省。
datas += collect_data_files('ttkbootstrap')

# ------------------------------------------------------------ hidden imports
hiddenimports = []
# Backend modules are picked at runtime by platform, so PyInstaller misses them.
for pkg in ('pynput', 'PyMemoryEditor', 'mss'):
    hiddenimports += collect_submodules(pkg)
hiddenimports += ['pywinctl', 'pywinbox', 'psutil']
# WebView 层：pywebview 在 guilib.py 里按平台挑模块，Windows 走 winforms +
# WebView2(EdgeChromium)。clr/pythonnet 的 DLL 由 hooks-contrib 收，这里只保证
# 模块本身不会被漏掉。
hiddenimports += [
    'webview.platforms.winforms',
    'webview.platforms.edgechromium',
    'clr',
    'pythonnet',
]

# ------------------------------------------------------------------- analysis
a = Analysis(
    [str(ROOT / 'packaging' / 'launcher.py')],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim the heavy scientific stack: nothing here needs Qt, tests or samples.
    excludes=[
        'matplotlib', 'scipy', 'pandas', 'IPython', 'notebook', 'jupyter',
        'pytest', 'setuptools', 'pip', 'tkinter.test', 'test', 'unittest',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

icon_arg = str(ico) if ico.exists() else None
version_arg = ROOT / 'packaging' / 'version_info.txt'
version_arg = str(version_arg) if version_arg.exists() else None

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='Sai2-Timelapse',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        icon=icon_arg,
        version=version_arg,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='Sai2-Timelapse',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon=icon_arg,
        version=version_arg,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name='Sai2-Timelapse',
    )
