"""Entry point used by PyInstaller.

The package uses relative imports, so the frozen app cannot use
``art_timelapse/gui.py`` directly as the script. This shim just imports
the package and calls its main().

``Sai2-Timelapse.exe --selftest`` runs a headless check of the pieces that are
easy to break when freezing (cv2 video encoding, gettext catalogs, memory
reading deps) and writes the result to the temp dir. Useful because the
windowed build has no console to print to.
"""
import json
import multiprocessing
import sys
import tempfile
import traceback
from pathlib import Path


def _selftest() -> int:
    result = {'ok': False, 'steps': {}}
    steps = result['steps']

    try:
        import cv2
        import numpy as np
        steps['import_cv2'] = cv2.__version__

        out = Path(tempfile.gettempdir()) / 'art_timelapse_selftest.mp4'
        if out.exists():
            out.unlink()
        writer = cv2.VideoWriter(str(out), cv2.VideoWriter.fourcc(*'mp4v'), 10, (64, 64))
        for i in range(5):
            writer.write(np.full((64, 64, 3), i * 40, dtype=np.uint8))
        writer.release()
        size = out.stat().st_size if out.exists() else 0
        steps['video_writer'] = {'path': str(out), 'bytes': size}

        cap = cv2.VideoCapture(str(out))
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        steps['video_reader_frames'] = frames
    except Exception:
        steps['video'] = traceback.format_exc()

    try:
        from art_timelapse import set_locale, _, get_locales_dir
        steps['locales_dir'] = str(get_locales_dir())
        set_locale('zh_CN')
        steps['zh_CN'] = _('SAI Recording')
        set_locale('en')
        steps['en'] = _('SAI Recording')
        set_locale('zh_CN')
    except Exception:
        steps['i18n'] = traceback.format_exc()

    try:
        import psutil
        from PyMemoryEditor import OpenProcess  # noqa: F401
        steps['memory_deps'] = f'psutil {psutil.__version__}'
    except Exception:
        steps['memory_deps'] = traceback.format_exc()

    try:
        # 打包版没有控制台，日志必须落盘，否则用户遇到问题一点线索都没有
        import logging
        from art_timelapse import get_log_path, default_log_path
        logging.info('selftest: log sink probe')
        path = get_log_path()
        steps['log_file'] = str(path) if path else f'(none, expected {default_log_path()})'
        steps['log_file_ok'] = bool(path and Path(path).exists())
    except Exception:
        steps['log_file'] = traceback.format_exc()
        steps['log_file_ok'] = False

    try:
        from art_timelapse import sai
        pid = sai.find_running_sai_pid()
        steps['sai_pid'] = pid
        if pid:
            api = sai.get_sai_api_from_pid(pid)
            steps['sai_version'] = api.version_name if api else None
    except Exception:
        steps['sai'] = traceback.format_exc()

    # The GUI scans for the SAI process inside asyncio.to_thread(). If that
    # worker cannot start, the task dies silently and the UI keeps showing
    # "SAI is not running" even though the process is right there.
    try:
        import asyncio

        async def _thread_probe():
            return await asyncio.to_thread(sai.find_running_sai_pid)

        steps['to_thread_pid'] = asyncio.run(_thread_probe())
    except Exception:
        steps['to_thread'] = traceback.format_exc()

    # WebView2 运行时是界面层的前提。缺了它程序会退回 Tk 界面，
    # 但那个回退在打包版里很容易因为某个模块没被收进来而静默失败，
    # 所以这里把三件事分开报：包能不能导入、运行时在不在、CLR 能不能加载。
    try:
        import webview  # noqa: F401
        steps['webview_import'] = 'ok'
    except Exception:
        steps['webview_import'] = traceback.format_exc()

    try:
        import winreg

        key = r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients'
        found = {}
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                name = winreg.EnumKey(root, index)
                try:
                    with winreg.OpenKey(root, name) as sub:
                        found[name] = winreg.QueryValueEx(sub, 'pv')[0]
                except OSError:
                    continue
        steps['webview2_runtime'] = found or 'not found'
    except Exception:
        steps['webview2_runtime'] = traceback.format_exc()

    try:
        import clr  # noqa: F401
        steps['clr'] = 'ok'
    except Exception:
        steps['clr'] = traceback.format_exc()

    try:
        from art_timelapse import titlebar
        steps['titlebar'] = 'ok' if titlebar._colorref('#1f2124') == 0x24211F else 'bad colorref'
    except Exception:
        steps['titlebar'] = traceback.format_exc()

    try:
        from pathlib import Path as _P
        from art_timelapse import webapp
        ui = _P(webapp.__file__).resolve().parent / 'webui'
        steps['webui_dir'] = str(ui)
        steps['webui_files'] = sorted(p.name for p in ui.iterdir()) if ui.is_dir() else 'MISSING'
    except Exception:
        steps['webui_files'] = traceback.format_exc()

    result['ok'] = (
        isinstance(steps.get('video_writer'), dict)
        and steps['video_writer']['bytes'] > 0
        and steps.get('zh_CN') == 'SAI 录制'
    )

    report = Path(tempfile.gettempdir()) / 'art_timelapse_selftest.json'
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['ok'] else 1


def _run():
    """先起 WebView 界面；WebView2 不可用时退回 Tk 界面。

    两套界面共用同一个后端（``sai`` / ``timelapse`` / ``store``），
    所以回退只是"换个壳"，功能和配置都不受影响。
    """
    try:
        from art_timelapse.webapp import main as web_main
        web_main()
        return
    except Exception:
        import logging
        logging.exception('WebView UI failed to start, falling back to the Tk UI')

    from art_timelapse.gui import main as tk_main
    tk_main()


if __name__ == '__main__':
    # Required so that any subprocess spawning (if it ever happens) does not
    # re-execute the whole GUI.
    multiprocessing.freeze_support()

    if '--selftest' in sys.argv:
        sys.exit(_selftest())

    try:
        _run()
    except Exception:
        # A windowed build has no console, so surface fatal startup errors
        # in a message box instead of dying silently.
        detail = traceback.format_exc()
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                f'Sai2-Timelapse failed to start:\n\n{detail[-1500:]}',
                'Sai2-Timelapse🔔',
                0x10,
            )
        except Exception:
            pass
        sys.exit(1)
