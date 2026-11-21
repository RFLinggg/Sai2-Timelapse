"""给系统标题栏上色。

WebView 的窗口是 WinForms 宿主，标题栏由 DWM 画，CSS 管不到。
不改的话，深色界面顶着一条白色标题栏，比原来 Tk 版还难看。

和 gui.py 里那份的区别：这里接 HWND，不依赖 Tk 的 ``winfo_id()``。
"""
from __future__ import annotations

import ctypes
import logging
import os

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36


def _colorref(hex_color: str) -> int | None:
    """'#RRGGBB' → Win32 的 0x00BBGGRR。"""
    try:
        value = hex_color.lstrip('#')
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
        return (b << 16) | (g << 8) | r
    except Exception:
        return None


def find_window(title: str) -> int | None:
    if os.name != 'nt':
        return None
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        return hwnd or None
    except Exception:
        return None


class _RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_ulong), ('rcMonitor', _RECT), ('rcWork', _RECT),
                ('dwFlags', ctypes.c_ulong)]


MONITOR_DEFAULTTONEAREST = 2
SW_RESTORE = 9


def window_metrics(hwnd: int):
    """返回 (left, top, right, bottom, 工作区高度)。

    工作区高度取窗口所在那块显示器的 rcWork（已扣掉任务栏），
    折叠面板撑高窗口时不能顶到任务栏底下去。
    """
    if os.name != 'nt' or not hwnd:
        return 0, 0, 900, 660, 660
    user32 = ctypes.windll.user32
    rect = _RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    work_h = rect.bottom - rect.top
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        work_h = info.rcWork.bottom - info.rcWork.top
    return rect.left, rect.top, rect.right, rect.bottom, work_h


def is_maximized(hwnd: int) -> bool:
    if os.name != 'nt' or not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.IsZoomed(hwnd))
    except Exception:
        return False


def apply(hwnd: int, bg: str, fg: str, border: str, dark: bool = True) -> str | None:
    """按给定颜色给标题栏上色，返回实际生效的方式（便于自检）。"""
    if os.name != 'nt' or not hwnd:
        return None
    try:
        dwm = ctypes.windll.dwmapi

        def set_attr(attr, value):
            buf = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(buf),
                                             ctypes.sizeof(buf)) == 0

        # Win11：逐项上色，能和主题完全对齐
        caption = _colorref(bg)
        if caption is not None and set_attr(_DWMWA_CAPTION_COLOR, caption):
            set_attr(_DWMWA_TEXT_COLOR, _colorref(fg))
            set_attr(_DWMWA_BORDER_COLOR, _colorref(border))
            return 'caption'

        # Win10 1809~1909 用的是 19 号属性
        for attr in (_DWMWA_USE_IMMERSIVE_DARK_MODE, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
            if set_attr(attr, 1 if dark else 0):
                return 'dark-mode'
        return None
    except Exception:
        logging.debug('Could not theme the title bar', exc_info=True)
        return None
