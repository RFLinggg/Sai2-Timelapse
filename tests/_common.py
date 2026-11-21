"""实机测试共用的进程 / 窗口 / 画布链表工具。

只做三件事，别往里加业务逻辑：

1. 找到正在跑的 ``sai2.exe``
2. 找到它的主窗口（按面积挑最大的 —— SAI 的「工具」「图层」都是独立顶层
   窗口，按标题找会抓到它们，Ctrl+N 就发进面板里了）
3. 读出画布链表，用来判断「现在到底有没有一张真画布」

**前置条件：SAI2 里得开着一张画。** 没有真画布时链表只剩占位节点，
``get_canvas_list()`` 会把它 ``[1:]`` 掉 —— 结果就是空。嗅探器报告里
最常见的失败原因就是这个，不是 offset 找错了。

本机没开 SAI2 时，可以把它的路径放进环境变量 ``SAI2_EXE``，
测试脚本会尝试启动它。
"""
import ctypes
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

import psutil

from art_timelapse import sai


class RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


def exe_path():
    """SAI2 的位置。只从环境变量读，不猜 —— 每个人的安装路径都不一样。"""
    raw = os.environ.get('SAI2_EXE', '').strip()
    return Path(raw) if raw else None


def find_sai2():
    for p in psutil.process_iter(['pid', 'name']):
        if (p.info['name'] or '').lower() == 'sai2.exe':
            return p.info['pid']
    return None


def ensure_sai2(timeout=30.0):
    """返回 (pid, started)。没开着且有 SAI2_EXE 就启动它。"""
    pid = find_sai2()
    if pid:
        return pid, False
    path = exe_path()
    if not path or not path.exists():
        print('SAI2 没开着，也没配 SAI2_EXE。请先打开 SAI2 并开一张画。')
        return None, False
    print(f'启动 {path}')
    subprocess.Popen([str(path)])
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        pid = find_sai2()
        if pid:
            return pid, True
    print('SAI2 没起来')
    return None, False


def main_window(pid):
    """按面积挑最大的可见顶层窗口，返回 (hwnd, RECT)。

    **先 ShowWindow(RESTORE) 再取矩形。** 最小化窗口的 GetWindowRect 返回
    (-32000, -32000) 那一套，拿它算中心点等于点在屏幕角落。
    """
    u = ctypes.windll.user32
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    best = []

    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        po = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(po))
        if po.value != pid:
            return True
        r = RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        area = (r.right - r.left) * (r.bottom - r.top)
        if area > 100 * 100:
            best.append((area, hwnd))
        return True

    u.EnumWindows(proto(cb), 0)
    if not best:
        return None, None
    best.sort(reverse=True)
    hwnd = best[0][1]
    u.ShowWindow(hwnd, 9)
    time.sleep(0.5)
    rect = RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    return hwnd, rect


def activate(hwnd, rect, mouse=None):
    """把 SAI2 切到前台，必要时点一下标题栏。

    ``SetForegroundWindow`` 只保证「窗口在最前」，不保证「输入焦点在它身上」，
    而且后台进程直接抢前台会被 Windows 静默拒绝（实测前台还是 QQ）。点标题栏
    落在 SAI2 自己的窗口内，不会画到画布上。
    """
    u = ctypes.windll.user32
    u.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    if u.GetForegroundWindow() == hwnd or mouse is None:
        return True
    from pynput.mouse import Button
    mouse.position = ((rect.left + rect.right) // 2, rect.top + 8)
    time.sleep(0.25)
    mouse.click(Button.left)
    time.sleep(0.8)
    return u.GetForegroundWindow() == hwnd


def read_chain(pid):
    """读画布链表，返回 (节点数, ['名字(宽x高)', ...])。

    类按 exe 的 MD5 从版本表里取，取不到就返回 (0, []) —— 这时应该先跑
    ``test_detect_live.py`` 让它探一遍。
    """
    cls = sai.get_sai_api_from_pid(pid)
    if cls is None:
        return 0, []
    proc_mod = sai.OpenProcess(pid=pid)
    with proc_mod as proc:
        api = cls(proc)
        head = sai.RPOINTER(sai.RPOINTER(api.SAICanvas))(
            api.base_address + cls.session_offset).get(proc)
        nodes = api.collect_canvases(head)
        items = []
        for n in nodes:
            try:
                name = n.get_name()
            except Exception:
                name = '?'
            try:
                size = f'{n.width}x{n.height}'
            except Exception:
                size = '?'
            items.append(f'{name!r}({size})')
        return len(nodes), items


def require_canvas(pid):
    """有真画布就返回 True，没有就把原因打出来。"""
    n, items = read_chain(pid)
    print(f'画布链 {n} 个: {", ".join(items) or "(空)"}')
    if n < 2:
        print('链表里只有占位节点。先在 SAI2 里打开一张画（或 Ctrl+N 新建），')
        print('确认画布显示出来了，再跑这个测试。')
        return False
    return True
