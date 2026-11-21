"""原生文件夹选择框（IFileOpenDialog，Windows Vista+ 的现代样式）。

为什么不直接用 pywebview 的 ``create_file_dialog``：它文档里要求「在主线程调用」，
而 WebView 的主线程被 GUI 循环占着，我们的 API 方法都跑在工作线程上。
与其赌它内部怎么marshal，不如自己调 COM —— 这个对话框本来就是「在哪个线程调、
就在哪个线程弹模态」的，不受 GUI 线程影响。

失败一律返回 ``None``，界面上仍然可以直接手输/粘贴路径。
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import POINTER, byref, c_void_p, wintypes

ole32 = ctypes.windll.ole32
shell32 = ctypes.windll.shell32

HRESULT = ctypes.c_long
CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040
FOS_PATHMUSTEXIST = 0x00000800
FOS_FILEMUSTEXIST = 0x00001000

SIGDN_FILESYSTEMPATH = 0x80058000

# IFileOpenDialog 的 vtable 序号（IUnknown 0-2 / IModalWindow 3 / IFileDialog 4-26）
VT_SHOW = 3
VT_SET_OPTIONS = 9
VT_SET_TITLE = 17
VT_GET_RESULT = 20
VT_ISHELLITEM_GET_DISPLAY_NAME = 5


class GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', ctypes.c_ulong),
        ('Data2', ctypes.c_ushort),
        ('Data3', ctypes.c_ushort),
        ('Data4', ctypes.c_ubyte * 8),
    ]

    def __init__(self, text):
        super().__init__()
        ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(self))


CLSID_FileOpenDialog = GUID('{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}')
IID_IFileOpenDialog = GUID('{d57c7288-d4ad-4768-be02-9d969532d960}')


def _method(ptr, index, restype, *argtypes):
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtable[index])


def _release(ptr):
    try:
        _method(ptr, 2, ctypes.c_ulong)(ptr)
    except Exception:
        pass


def _show_dialog(title: str, folder: bool, default_path: str = ''):
    """弹框并返回选中的绝对路径；用户取消或出错返回 None。"""
    initialized = False
    result = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if result == 0 or result == 1:          # S_OK / S_FALSE
        initialized = True
    elif result != RPC_E_CHANGED_MODE:
        logging.debug('CoInitializeEx failed: %s', result)

    dialog = c_void_p()
    item = c_void_p()
    path_ptr = ctypes.c_wchar_p()
    try:
        hr = ole32.CoCreateInstance(byref(CLSID_FileOpenDialog), None,
                                    CLSCTX_INPROC_SERVER, byref(IID_IFileOpenDialog),
                                    byref(dialog))
        if hr != 0 or not dialog:
            logging.warning('CoCreateInstance(FileOpenDialog) failed: %s', hr)
            return None

        options = wintypes.DWORD()
        _method(dialog, 10, HRESULT, POINTER(wintypes.DWORD))(dialog, byref(options))
        flags = options.value | FOS_FORCEFILESYSTEM
        flags |= FOS_PICKFOLDERS | FOS_PATHMUSTEXIST if folder else FOS_FILEMUSTEXIST
        _method(dialog, VT_SET_OPTIONS, HRESULT, wintypes.DWORD)(dialog, flags)

        if title:
            _method(dialog, VT_SET_TITLE, HRESULT, ctypes.c_wchar_p)(dialog, title)

        # Show(NULL)：不挂父窗口。父窗口属于另一个线程，挂上去反而可能弹到后面。
        hr = _method(dialog, VT_SHOW, HRESULT, wintypes.HWND)(dialog, None)
        if hr != 0:      # 用户取消是 0x800704C7，同样走这里
            return None

        hr = _method(dialog, VT_GET_RESULT, HRESULT, POINTER(c_void_p))(dialog, byref(item))
        if hr != 0 or not item:
            return None
        hr = _method(item, VT_ISHELLITEM_GET_DISPLAY_NAME, HRESULT,
                     wintypes.DWORD, POINTER(ctypes.c_wchar_p))(item, SIGDN_FILESYSTEMPATH,
                                                                byref(path_ptr))
        if hr != 0 or not path_ptr.value:
            return None
        return path_ptr.value
    except Exception:
        logging.exception('Folder dialog failed')
        return None
    finally:
        if path_ptr:
            ole32.CoTaskMemFree(path_ptr)
        if item:
            _release(item)
        if dialog:
            _release(dialog)
        if initialized:
            ole32.CoUninitialize()


def pick_folder(title='', default_path='') -> str | None:
    return _show_dialog(title, folder=True, default_path=default_path)


def pick_file(title='', default_path='') -> str | None:
    return _show_dialog(title, folder=False, default_path=default_path)
