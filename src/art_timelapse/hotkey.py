"""全局热键监听。独立成模块，WebView 版不需要导入 tkinter 也能用。"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
from ctypes import wintypes

from . import APP_NAME


class HotkeyListener:
    """全局热键：在独立线程里跑消息循环。

    Tk 的主循环会把 ``WM_HOTKEY`` 当成不认识的消息丢掉，所以不能注册到 Tk 窗口上。
    WebView（WinForms）那边同理，消息循环不归我们管。
    这里用 ``RegisterHotKey(None, ...)`` 把热键绑到本线程的消息队列，
    再由本线程的 ``GetMessageW`` 取出来。
    """

    HOTKEY_ID = 1
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    VK_S = 0x53

    def __init__(self, callback, vk=VK_S, modifiers=None):
        self.callback = callback
        self.vk = vk
        self.modifiers = (self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT
                          if modifiers is None else modifiers)
        self.thread = None
        self.thread_id = None
        self.registered = False
        # start() 只是拉起线程，注册结果要等线程跑起来才知道。
        # 界面要按结果决定是否显示提示，所以得有个「结果已出」的信号。
        self.ready = threading.Event()

    def start(self):
        if os.name != 'nt':
            return
        self.thread = threading.Thread(target=self._run, name=f'{APP_NAME}-hotkey', daemon=True)
        self.thread.start()

    def _run(self):
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.thread_id = kernel32.GetCurrentThreadId()
        try:
            self.registered = bool(user32.RegisterHotKey(None, self.HOTKEY_ID, self.modifiers, self.vk))
            self.ready.set()
            if not self.registered:
                # 被别的程序占用了，不影响主功能，只是没有快捷键
                logging.warning('Global hotkey Ctrl+Alt+S is already taken by another application')
                return
            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:   # 0 = WM_QUIT，-1 = 出错
                    break
                if msg.message == self.WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                    try:
                        self.callback()
                    except Exception:
                        logging.exception('Hotkey callback failed')
        except Exception:
            logging.exception('Hotkey thread failed')
        finally:
            self.ready.set()
            try:
                user32.UnregisterHotKey(None, self.HOTKEY_ID)
            except Exception:
                pass

    def stop(self):
        if self.thread_id:
            try:
                ctypes.WinDLL('user32', use_last_error=True).PostThreadMessageW(
                    self.thread_id, self.WM_QUIT, 0, 0)
            except Exception:
                logging.debug('Could not stop hotkey thread', exc_info=True)
