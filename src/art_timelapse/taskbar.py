"""任务栏进度条（Windows ``ITaskbarList3``）。

为什么需要它：录制的时候用户人在 SAI 里画画，根本不看这个窗口 —— 界面里
任何动画都是白给。任务栏图标上那条进度才是唯一有效的「还在录」反馈。

三种状态：

* **录制中** → 不确定态（``TBPF_INDETERMINATE``）。录制没有终点，编一个总数
  是骗人；不确定态在任务栏上是一段来回滚动的绿条，表达「正在进行」刚好。
* **暂停** → 暂停态（``TBPF_PAUSED``）。黄色静止，和「还在录」一眼区分开。
* **导出** → 正常态 + 真实百分比。导出有明确的总帧数，这个进度是真的。
* **结束** → 清空（``TBPF_NOPROGRESS``）。

全部走 ctypes 手写 COM，不引入 pywin32 —— 打包时多一个依赖就多一份体积和
兼容风险，而这里总共只用到三个方法。

调用约定：``set_state`` / ``set_progress`` 都是「尽力而为」。任何一步失败
（COM 没初始化、窗口还没有任务栏按钮、系统太老）都只记一条 debug 日志并返回
False —— 任务栏反馈不该有能力把录制搞挂。
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import POINTER, byref, c_void_p, c_ulong, c_ushort, c_ubyte
from ctypes import wintypes

# CLSID_TaskbarList / IID_ITaskbarList3
#
# 这两个值**必须**和本机 SDK 的 shobjidl_core.h 一致，别凭记忆写。第一版把
# ITaskbarList3 的末段记成了 ...9e9f8a5eea84（正确是 ...9e9f8a5eefaf），
# CoCreateInstance 一路回 E_NOINTERFACE —— 看着像"系统不支持"，其实只是抄错。
# 核对方法：
#   grep -h "ea1afb91" "$WindowsSdkDir/Include/*/um/shobjidl_core.h"
_CLSID = '{56FDF344-FD6D-11d0-958A-006097C9A090}'
_IID = '{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}'

# TBPFLAG
TBPF_NOPROGRESS = 0x0
TBPF_INDETERMINATE = 0x1
TBPF_NORMAL = 0x2
TBPF_ERROR = 0x4
TBPF_PAUSED = 0x8

# ITaskbarList3 的 vtable 槽位。前 9 个来自 IUnknown / ITaskbarList / ITaskbarList2。
_VT_HRINIT = 3
_VT_SET_PROGRESS_VALUE = 9
_VT_SET_PROGRESS_STATE = 10

_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2
_HRESULT = ctypes.c_long


class _GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', c_ulong),
        ('Data2', c_ushort),
        ('Data3', c_ushort),
        ('Data4', c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    """把 ``{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`` 解析成内存里的 GUID 结构。

    注意别走 ``uuid.UUID(text).bytes`` + ``int.from_bytes(..., 'little')`` 那条路：
    GUID 的字符串表示里前三段已经是**大端数值**，再按小端读一遍就变成
    ``0x44F3FD56``，``CoCreateInstance`` 直接回 ``REGDB_E_CLASSNOTREG``
    （看起来像"系统没装任务栏组件"，其实只是字节序反了）。
    前三段按大端转整数、后两段按原样塞 8 字节，ctypes 会自己处理内存布局。
    """
    parts = text.strip('{}').split('-')
    value = _GUID()
    value.Data1 = int(parts[0], 16)
    value.Data2 = int(parts[1], 16)
    value.Data3 = int(parts[2], 16)
    value.Data4 = (c_ubyte * 8)(*bytes.fromhex(parts[3] + parts[4]))
    return value


class _Taskbar:
    """一次创建、长期持有的 COM 对象。

    ``ITaskbarList3`` 是进程级服务，每次改状态都重新 ``CoCreateInstance``
    没必要（还容易在非主线程上踩初始化问题），所以懒创建后一直留着。
    """

    def __init__(self):
        self._ptr = None
        self._failed = False
        self._state = None
        self._last = None

    # ------------------------------------------------------------ COM
    @staticmethod
    def _vtable(ptr, slot: int, *argtypes):
        """取出 vtable 里第 ``slot`` 个方法，返回一个可调用对象。

        第一个参数永远是 ``this``，所以 proto 的签名是 ``(c_void_p, *argtypes)``。
        """
        table = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
        return ctypes.WINFUNCTYPE(_HRESULT, c_void_p, *argtypes)(table[slot])

    def _ensure(self) -> bool:
        if self._ptr is not None:
            return True
        if self._failed:
            return False
        try:
            ole32 = ctypes.WinDLL('ole32')
            ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
            ole32.CoInitializeEx.restype = _HRESULT
            ole32.CoCreateInstance.argtypes = [
                POINTER(_GUID), c_void_p, wintypes.DWORD,
                POINTER(_GUID), POINTER(c_void_p),
            ]
            ole32.CoCreateInstance.restype = _HRESULT

            # 已经初始化过会返回 S_FALSE(1)，不是错误；忽略返回值即可。
            # 单线程套间是任务栏 API 的常规用法。
            ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)

            ptr = c_void_p()
            hr = ole32.CoCreateInstance(
                byref(_guid(_CLSID)), None, _CLSCTX_INPROC_SERVER,
                byref(_guid(_IID)), byref(ptr),
            )
            if hr < 0 or not ptr:
                logging.debug('Taskbar: CoCreateInstance failed hr=0x%08x', hr & 0xFFFFFFFF)
                self._failed = True
                return False

            if self._vtable(ptr, _VT_HRINIT)(ptr) < 0:
                logging.debug('Taskbar: HrInit failed')
                self._failed = True
                return False

            self._ptr = ptr
            return True
        except Exception:
            logging.debug('Taskbar: COM unavailable', exc_info=True)
            self._failed = True
            return False

    # ------------------------------------------------------------ 对外
    def set_state(self, hwnd, flag: int) -> bool:
        """只改状态（不确定 / 暂停 / 清空）。同一个状态重复设置会被跳过。"""
        if not hwnd:
            return False
        if self._state == flag and self._last is None:
            return True
        if not self._ensure():
            return False
        try:
            call = self._vtable(self._ptr, _VT_SET_PROGRESS_STATE, wintypes.HWND, ctypes.c_int)
            hr = call(self._ptr, wintypes.HWND(hwnd), flag)
            if hr < 0:
                logging.debug('Taskbar: SetProgressState(0x%x) hr=0x%08x', flag, hr & 0xFFFFFFFF)
                return False
            self._state = flag
            self._last = None
            return True
        except Exception:
            logging.debug('Taskbar: SetProgressState raised', exc_info=True)
            return False

    def set_progress(self, hwnd, done: int, total: int) -> bool:
        """真实进度。``total`` 为 0 时退回不确定态，免得除零。"""
        if not hwnd:
            return False
        total = max(int(total or 0), 0)
        if total <= 0:
            return self.set_state(hwnd, TBPF_INDETERMINATE)
        done = max(0, min(int(done), total))
        if not self._ensure():
            return False
        # 状态还是「不确定」的话，先切成正常态，否则进度值不显示
        if self._state != TBPF_NORMAL:
            if not self.set_state(hwnd, TBPF_NORMAL):
                return False
        key = (done, total)
        if self._last == key:
            return True
        try:
            call = self._vtable(
                self._ptr, _VT_SET_PROGRESS_VALUE,
                wintypes.HWND, ctypes.c_ulonglong, ctypes.c_ulonglong,
            )
            hr = call(self._ptr, wintypes.HWND(hwnd), done, total)
            if hr < 0:
                logging.debug('Taskbar: SetProgressValue hr=0x%08x', hr & 0xFFFFFFFF)
                return False
            self._last = key
            return True
        except Exception:
            logging.debug('Taskbar: SetProgressValue raised', exc_info=True)
            return False

    def clear(self, hwnd) -> bool:
        return self.set_state(hwnd, TBPF_NOPROGRESS)


_instance = _Taskbar()


def set_state(hwnd, flag: int) -> bool:
    return _instance.set_state(hwnd, flag)


def set_progress(hwnd, done: int, total: int) -> bool:
    return _instance.set_progress(hwnd, done, total)


def clear(hwnd) -> bool:
    return _instance.clear(hwnd)
