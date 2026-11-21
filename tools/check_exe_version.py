"""读取 exe 的版本资源，确认 PyInstaller 的 version 参数生效。"""
import sys
from pathlib import Path

import win32api

exe = Path(sys.argv[1] if len(sys.argv) > 1 else 'dist/Sai2-Timelapse.exe').resolve()
print('file:', exe, f'({exe.stat().st_size / 1024 / 1024:.1f} MB)')

info = win32api.GetFileVersionInfo(str(exe), '\\')
ms, ls = info['FileVersionMS'], info['FileVersionLS']
print('FileVersion : %d.%d.%d.%d' % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF))
pms, pls = info['ProductVersionMS'], info['ProductVersionLS']
print('ProdVersion : %d.%d.%d.%d' % (pms >> 16, pms & 0xFFFF, pls >> 16, pls & 0xFFFF))

base = '\\StringFileInfo\\080404b0\\'
for key in ('FileDescription', 'ProductName', 'CompanyName',
            'LegalCopyright', 'OriginalFilename'):
    try:
        print(f'{key:16}: {win32api.GetFileVersionInfo(str(exe), base + key)}')
    except Exception as exc:  # noqa: BLE001
        print(f'{key:16}: <{exc}>')
