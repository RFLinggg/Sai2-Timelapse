"""WebView 版界面后端。

和 Tk 版的区别只在**界面层**：``sai.py`` / ``timelapse.py`` / ``store.py`` 一行没改。
这里负责三件事：

1. 把界面状态整理成一个 JSON 快照，前端定时来拉；
2. 暴露几个动作给前端调用（开始/停止录制、导出、选目录……）；
3. 用独立线程跑 asyncio 事件循环 —— WebView 的 GUI 循环占着主线程，
   录制/导出这些协程不能挂在主线程上。

**为什么是"前端轮询"而不是"后端推"**：pywebview 的 ``evaluate_js`` 从别的线程调用
在不同后端上行为不一致，而轮询 150ms 一次、每次几百字节，代价可以忽略，
换来的是「不管后端怎么变都不会卡死界面」。日志用游标增量拉，不重复传全量。
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import logging
import os
import psutil
import re
import shutil
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import webview

from . import timelapse, sai, set_locale, get_log_path, _ as tr
from . import auto_detect
from . import filedialog, titlebar, taskbar
from . import APP_NAME, SETTINGS_FILE, __version__, window_title
from .hotkey import HotkeyListener
from .store import Store
from .util import clamp_number, format_duration

# 窗口标题（带铃铛和版本）。DWM 上色靠 FindWindowW 按标题找窗口，
# 所以建窗和找窗必须用同一个字符串 —— 以前这里是两处独立的常量。
APP_TITLE = window_title()
LOG_LIMIT = 3000
MIN_WIDTH, MIN_HEIGHT = 860, 560
DEFAULT_SIZE = (980, 660)

# 画布预览的抓图边长（像素）。预览框 196px 宽，这里取 400px：
# 以前写死 100px，等于把 100px 的图放大两倍塞进 196px 的框，糊是必然的；
# 400px 在 2x 高分屏上按 196 CSS 像素显示也还有余量。再大只是白烧内存和带宽。
PREVIEW_PX = 400

# 标题栏由 DWM 画，CSS 管不到。这三个色必须和 webui/style.css 里的
# --bg / --text / --border 保持一致，否则标题栏和界面之间会有一条色差。
TITLEBAR_COLORS = {
    'sai-dark': ('#111316', '#e4e7ea', '#262b30', True),
    'sai-light': ('#f3f3f4', '#1a1c1f', '#e3e5e8', False),
}

# 键名和默认值必须和 Tk 版完全一致 —— 两边共用 ~/.art-timelapse，
# 换界面不该让用户的帧目录、导出设置、窗口位置丢一遍。
DEFAULTS = {
    'frames_file': '',
    'auto_split_sai': 500,
    'recording_type': 0,
    'recording_custom_container': '',
    'recording_custom_codec': '',
    'export_type': 0,
    'export_custom_container': '',
    'export_custom_codec': '',
    'sai_version_override': 0,
    'sai_canvas': '',
    'image_size_limit': 1920,
    'export_time_limit': 0.0,
    'export_fps': 30.0,
    'export_preview_last_frame': True,
    'export_preview_duration': 0.0,
    'export_user_recording': True,
    'selected_tab': 0,
    'theme': 'sai-dark',
    'lang': 'zh_CN',
    'win_size': [600, 600],
    'win_geometry': '',
    'win_state': 'normal',
    # 悬浮条：默认开。它是「人在 SAI 里画画」时唯一能看到的控制入口。
    'floatbar': True,
    'float_pos': '',
    # 悬浮条的缩放比（0.45~1.0）。BASE 302x58 是上限，默认 0.62 约 187x36。
    'float_scale': 0.5,
    # 接着上次的会话继续录。默认关：原来每次录制都是一段新的，
    # 导出只读最新那个会话（resolve_frames_dir），这个行为不能悄悄改掉。
    'append_last_session': False,
}

# 界面上会出现的文案。值就是 .po 里的 msgid，翻译走已有的 .mo，
# 不重新做一套 i18n。
MESSAGES = {
    'tab_sai': 'SAI Recording',
    'tab_export': 'Export Video',
    'advanced': 'Advanced settings',
    'status': 'Status',
    'clear': 'Clear',
    'copy': 'Copy',
    'open_log': 'Open log file',
    'log_empty': 'No log entries yet. Start recording and progress shows up here.',
    'frames_unit': 'frames',
    'frames_path': 'Save folder',
    'canvas': 'Canvas',
    'sai_version': 'SAI version detected',
    'sai_version_override': 'SAI version override',
    'sai_not_running': 'SAI is not running',
    'auto_split': 'Auto split count',
    'capture_size': 'Capture size limit',
    'canvas_preview': 'Canvas preview',
    'video_type': 'Video type',
    'custom_container': 'Custom container',
    'custom_codec': 'Custom codec',
    'restore_defaults': 'Restore default settings',
    'export_fps': 'Export FPS',
    'export_time_limit': 'Export time limit',
    'this_session': 'This session',
    'preview_last_frame': 'Preview last frame first',
    'preview_duration': 'Preview duration',
    'use_recording_type': 'Use recording video type',
    'start_recording': 'Start Recording',
    'stop_recording': 'Stop Recording',
    'pause': 'Pause',
    'resume': 'Resume',
    'paused': 'Paused',
    'recording_paused': 'Recording paused',
    'recording_resumed': 'Recording resumed',
    'confirm_stop_title': 'Stop recording?',
    'confirm_stop_body': '{} frames recorded · {}. You can export it from the Export tab afterwards.',
    'cancel': 'Cancel',
    'export': 'Export',
    'stop_exporting': 'Stop Exporting',
    'saved_to': 'Saved to',
    'open': 'Open',
    'select': 'Select',
    'recording': 'Recording',
    'no_preview': 'No preview',
    'no_frames': 'No frames recorded yet',
    'theme': 'Theme:',
    'language': 'Language',
    # 反馈渠道：号码本身写在前端 DOM 和 app.js 里，这里只要一个标签，
    # 免得把号码散进翻译文件（改一次要改三处 .po）。
    'feedback': 'Feedback QQ group',
    'floatbar': 'Floating bar',
    # 会话接续 / 归档
    'append_last_session': 'Append to last session',
    'archive_frames': 'Archive frames',
    'append_to_session': 'Continuing session {1} ({0} frames already recorded)',
    'new_session_hint': 'Last session is {0}; this recording starts a new clip. Turn on "Append to last session" to continue that one instead.',
    'append_size_mismatch': 'Canvas is {}x{} but the existing recording is {}x{}; on export that part will be scaled and letterboxed.',
    'archived_frames': 'Archived {} frames from session {}',
    'error': 'Error',
    'confirm_restore': 'All settings except the frames folder, theme, language and window position will be reset. Continue?',
    'restored': 'Settings restored to defaults.',
    'restored_n': 'Settings restored to defaults ({} items)',
    'no_canvases': 'There are no canvases open',
    'no_sai': 'No valid SAI process found',
    'folder_missing': 'Folder does not exist: {}',
    'video_mp4': 'mp4/avc1 - Works with most websites, including Twitter',
    'video_webm': 'webm/vp80 - Better color quality, but larger file size',
    'video_custom': 'Use custom container/codec',
    'original_size': 'Original size',
    'override': '(Override)',
    'frames_summary': '{} frames · {} source → {} video',
    'sampled': '(sampled)',
    # 导出前先把成片名摆出来，免得导完才发现叫 20261101_143611
    'will_save_as': 'saves as {}',
    # 首次启动的引导
    'setup_title': 'Choose a save folder',
    'setup_body': 'Recording clips and exported videos both live in this one folder. You can change it later on the Recording tab.',
    'setup_pick': 'Choose folder…',
    'setup_use': 'Use this folder',
    'no_save_dir': 'Choose a save folder first — nothing is recorded without one.',
    # 提示气泡
    'tip_frames': 'One folder for everything. Each recording gets a timestamped subfolder holding the raw clips, and the exported video is saved next to it. Existing files are never overwritten - a _1, _2 suffix is added instead.',
    'tip_canvas': 'Select which open SAI canvas to record from.',
    'tip_override': 'If the running SAI version cannot be detected, the selected override will be used.',
    'tip_size': 'Captured images will be scaled down to fit this size. Larger means sharper video but slower capture and bigger files.',
    'tip_split': 'Automatically split video output after this many frames. Enter 0 or leave blank to disable. This is useful to mitigate the chance of missing or corrupt video data after a system crash.',
    'tip_video': 'Useful container/codec presets.',
    'tip_container': 'If you want to use a container that is not listed above, whatever is supported by OpenCV and FFMPEG.',
    'tip_codec': 'If you want to use a codec that is not listed above, whatever is supported by OpenCV and FFMPEG.',
    'tip_fps': 'Set the FPS of the exported video. 30 is a common default.',
    'tip_limit': 'Maximum time the exported video should be. Enter 0 or leave blank for no time limit.',
    'tip_preview_first': 'Display the last frame at the beginning of the exported video, like a preview of the finished timelapse.',
    'tip_preview_dur': 'The duration of the preview frame in seconds. Enter 0 or leave blank to display for only a single frame. This adds on to the final export duration.',
    'tip_use_recording': 'Use the same video options as the recording tabs and ignore the below settings.',
    'tip_hotkey': 'Tip: Ctrl+Alt+S starts / stops recording globally, even while SAI has focus.',
    'hotkey_hint': 'Ctrl+Alt+S starts / stops recording, even while SAI has focus.',
}


class QueueLogHandler(logging.Handler):
    """把日志塞进环形缓冲，前端按游标增量拉取。"""

    # pywebview 自己会往 root logger 里写一堆 WebView2 的 COM 报错
    # （跨线程读 ZoomFactor 之类），跟用户无关，别让它污染日志区。
    IGNORED_LOGGERS = ('pywebview', 'webview')

    def __init__(self, backend):
        super().__init__()
        self.backend = backend

    def emit(self, record):
        if record.name.split('.')[0] in self.IGNORED_LOGGERS:
            return
        try:
            self.backend.push_log(self.format(record), record.levelname)
        except Exception:
            pass


class Backend:
    def __init__(self):
        self.store = Store(SETTINGS_FILE, DEFAULTS)
        # 导出目录已经并进「保存位置」：成片就落在会话子目录旁边。
        # 老配置里可能还留着这两个键，清掉，别让它们以后又被读出来。
        self.store.data.pop('export_dir', None)
        self.store.data.pop('export_file_var', None)
        self.window = None
        self.loop = None
        self._loop_thread = None
        self._closing = False

        self._log = deque(maxlen=LOG_LIMIT)
        self._log_id = 0

        # SAI 探测
        self.sai_proc = None
        self.current_pid = None
        self.last_pid = None
        self._sai_retry_after = 0.0
        self._sai_detect_failures = 0
        self._sai_status = 'none'     # none | running
        self._sai_version = ''
        self._sai_override = False
        # 未登记版本的自动探测（auto_detect.detect）。只有 MD5 查不到、且是 64 位
        # sai2.exe 时才会走到这里；探测在后台线程跑，最长 20 秒。
        # _auto_detect_pid 记录「探过哪个进程」，换了 pid 就重置 —— 用户重开 SAI
        # 是另一个进程，参数得重测。同一个 pid 失败后按 _auto_detect_next_try 退避，
        # 否则 0.4 秒一轮的检测循环会一直起新任务。
        self._auto_detect_task = None
        self._auto_detect_pid = None
        self._auto_detect_hash = ''
        self._auto_detect_next_try = 0.0
        self._auto_detect_failures = 0
        self.canvases = []
        self._canvas_rev = 0
        # 上一次列画布失败的原因。同样的错误只在第一次记日志，否则每 150ms 轮询
        # 会把日志刷满（画布读不出来时这个函数会一直抛）。
        self._canvas_error = ''
        self.preview_data = None
        self.preview_size = None
        self._preview_task = None
        # 预览的两个计数器：
        #   _preview_seq 领号防竞态 —— 连着切两个画布时，先发的抓图可能后完成，
        #                没有序号它会把新画布的图盖掉。
        #   _preview_rev 每次真的换了图就 +1，前端凭它判断「要不要重画」。
        #                只比 data URL 不可靠：同一张画布重抓可能得到完全相同的
        #                字符串，而切换画布也可能拿到同一张图。
        self._preview_seq = 0
        self._preview_rev = 0

        # 操作状态
        self.operation = None          # None | 'record' | 'export'
        self.operation_task = None
        self.record_frames = 0
        self.record_started = 0.0
        self.export_fraction = 0.0
        self.export_label = ''
        self.export_result = ''
        # 成片卡片用的元信息：大小、时长、帧数、缩略图。导出成功后才有内容，
        # 空字典表示「这次没有可展示的结果」。
        self.export_info = {}
        self.summary = ''
        # 与 summary 同源（scan_frames 的帧数），但保持数字形态给前端用
        self.summary_frames = 0
        # 素材被切成几个文件。界面上必须讲清楚「目录里那堆不是成片」——
        # 同一个问题 2026-11-20 一天被问了两回。
        self.summary_segments = 0
        self._summary_dirty = True

        # 录制暂停。暂停不结束会话：writer 和帧目录都留着，恢复后接着写同一个视频。
        self.record_paused = False
        self._pause_event = None       # asyncio.Event，置位 = 暂停中
        self._paused_total = 0.0       # 累计已暂停时长（用来扣 elapsed）
        self._pause_started = 0.0      # 本次暂停的起点

        self.hotkey = None
        self.hotkey_ok = False
        self._titlebar_mode = None
        self._titlebar_hwnd = None
        # 任务栏进度要用的窗口句柄。和 _titlebar_hwnd 分开存：DWM 上色失败时
        # 那个会是 None，但任务栏进度照样能用。
        self._taskbar_hwnd = None

        # 界面重绘的版本号：设置被后端改动（恢复默认、切语言）时 +1，
        # 前端发现版本变了就重新拉一次 settings
        self.settings_rev = 0

        self.handler = QueueLogHandler(self)
        self.handler.setFormatter(logging.Formatter('%(message)s'))

        # 悬浮条（独立 Tk 线程）。WebView 起来之前不创建：进程的 DPI 感知是
        # WebView2 建立时才设好的，先起 Tk 会拿到错误的缩放比。
        self.floatbar = None

    # ------------------------------------------------------------ 生命周期
    def start(self):
        set_locale(self.store.get('lang') or 'zh_CN')
        logging.getLogger().addHandler(self.handler)
        self._loop_thread = threading.Thread(target=self._run_loop, name=f'{APP_NAME}-async', daemon=True)
        self._loop_thread.start()
        self.hotkey = HotkeyListener(lambda: self.submit(self.toggle_record))
        self.hotkey.start()
        # 热键是「不用切回窗口」才成立的功能，注册结果决定那行提示显不显示。
        # start() 只是拉起线程，要等线程报出结果。
        threading.Thread(target=self._await_hotkey, name=f'{APP_NAME}-hotkey-ready',
                         daemon=True).start()
        self._log_startup_info()

    def _await_hotkey(self):
        if self.hotkey.ready.wait(3.0):
            self.hotkey_ok = bool(self.hotkey.registered)
            if self.hotkey_ok:
                logging.info(tr('Global hotkey: {} to start / stop recording').format('Ctrl+Alt+S'))

    def _sync_floatbar(self):
        """按设置起停悬浮条。

        它跑在独立 Tk 线程里，所以「关掉」必须真的让那个线程退出、并把引用清掉 ——
        只记一个布尔标志的话，再打开会同时存在两个悬浮条，都置顶，叠在一起。
        """
        want = bool(self.store.get('floatbar'))
        if want and self.floatbar is None:
            try:
                from .floatbar import FloatBar
                bar = FloatBar(self)
                bar.start()
                self.floatbar = bar
            except Exception:
                logging.exception('Could not start the floating bar')
                self.floatbar = None
        elif not want and self.floatbar is not None:
            try:
                self.floatbar.stop()
            except Exception:
                logging.debug('Could not stop the floating bar', exc_info=True)
            self.floatbar = None

    def _log_startup_info(self):
        session_type = os.environ.get('XDG_SESSION_TYPE', '')
        if session_type.lower() == 'wayland':
            logging.warning(tr('Possible Wayland session is running because the following environment variable was set:'))
            logging.warning(tr('Wayland sessions are currently not supported by dependencies mss and pywinctl.'))
        # 7 个版本名逐条 INFO 会把真正要看的「SAI 未运行」顶出视野，
        # 清单降到 DEBUG，INFO 只留一行。
        logging.info(tr('Supported SAI versions: {}').format(len(sai.sai_api_lookup)))

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._detect_loop())
        self.loop.create_task(self._summary_loop())
        self.loop.run_forever()

    def submit(self, coro_func, *args):
        """从任意线程把一个协程丢进事件循环。"""
        if self.loop is None or self._closing:
            return None
        return asyncio.run_coroutine_threadsafe(coro_func(*args), self.loop)

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        if self.hotkey is not None:
            self.hotkey.stop()
        if self.floatbar is not None:
            self.floatbar.stop()
        self.store.save()
        # 先让正在跑的录制/导出收尾，再停事件循环。顺序反了的话
        # ``with InputTracker(...)`` / ``with VideoSequenceWriter(...)`` 永远不退出。
        self._unwind_operation()
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def _unwind_operation(self, timeout=2.5):
        """关窗前把正在跑的录制/导出**同步**收干净。

        不这么做的话，录制协程被直接丢掉，它持有的两个 with 块永远不会退出：

        · ``InputTracker`` 里的 pynput 鼠标监听是一个 **WH_MOUSE_LL 低级钩子**。
          进程带着钩子退出，Windows 之后每条鼠标事件都要等它超时（默认几百毫秒），
          表现就是「关掉程序后鼠标卡好几秒」。
        · ``VideoSequenceWriter`` 不 close，正在写的 mp4 会留下一个没写完的尾巴。

        所以这里 cancel 之后要**等它真的结束**，不能只 cancel 就走。
        """
        if self.loop is None or self.loop.is_closed():
            return
        task = self.operation_task
        if task is None or task.done():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._cancel_and_wait(task), self.loop)
            fut.result(timeout)
        except Exception:
            logging.debug('Could not unwind the running operation', exc_info=True)

    async def _cancel_and_wait(self, task, timeout=2.0):
        task.cancel()
        try:
            await asyncio.wait({task}, timeout=timeout)
        except Exception:
            logging.debug('Operation task did not finish cleanly', exc_info=True)

    # ------------------------------------------------------------ 日志
    def push_log(self, text, level='INFO'):
        self._log_id += 1
        self._log.append({
            'id': self._log_id,
            't': time.strftime('%H:%M:%S'),
            'text': text,
            'level': level.lower(),
        })

    def clear_log(self):
        self._log.clear()
        self._log_id += 1
        # 清空也要推进 id：否则前端拿着旧游标会以为「没有新日志」，
        # 但界面已经空了，下一行日志就再也进不去。
        self._log.append({'id': self._log_id, 't': time.strftime('%H:%M:%S'),
                          'text': '', 'level': 'clear'})

    # ------------------------------------------------------------ 文案
    def strings(self):
        return {key: tr(msgid) for key, msgid in MESSAGES.items()}

    # ------------------------------------------------------------ 状态
    def status(self):
        op = self.operation
        elapsed = 0.0
        if op == 'record' and self.record_started:
            # 暂停的时间不算进录制时长：用户看的是「录了多久」，不是「挂了多久」。
            elapsed = time.time() - self.record_started - self._paused_total
            if self.record_paused and self._pause_started:
                elapsed -= time.time() - self._pause_started
            elapsed = max(0.0, elapsed)
        return {
            'sai': {
                'state': self._sai_status,
                'version': self._sai_version,
                'override': self._sai_override,
                'canvases': list(self.canvases),
                'rev': self._canvas_rev,
            },
            'op': {
                'mode': op,
                'frames': self.record_frames,
                'elapsed': round(elapsed, 1),
                'paused': bool(self.record_paused),
                'progress': round(self.export_fraction, 4) if op == 'export' else 0.0,
                'label': self.export_label,
            },
            'export': {'result': self.export_result, 'summary': self.summary,
                       # 原始帧数，给前端判断「要不要在导出前拦一下」用。
                       # summary 是格式化好的文本，前端拿它没法做判断。
                       'frames': self.summary_frames,
                       # 素材分了几段。>1 时前端补一句「导出会合成一支」，
                       # 否则用户会以为那堆十几秒的文件就是导出结果。
                       'segments': self.summary_segments,
                       'info': self.export_info},
            'preview_rev': self._preview_rev,
            'preview_size': self.preview_size,
            'hotkey': {'ok': self.hotkey_ok, 'text': tr(MESSAGES['hotkey_hint'])},
            'settings_rev': self.settings_rev,
        }

    def _sync_taskbar(self):
        """把当前操作同步到任务栏图标。

        这是录制期间**唯一有效**的反馈：用户人在 SAI 里画画，根本不看这个窗口，
        界面里放什么动画都是白给。任务栏图标上那条进度才是他会瞥一眼的东西。

        * 录制中 → 不确定态（来回滚动的绿条）。录制没有终点，编一个总数是骗人。
        * 暂停   → 暂停态（黄色静止），和「还在录」一眼区分。
        * 导出   → 真实百分比。导出有明确总帧数，这个进度是真的。
        * 空闲   → 清空。

        放在 poll() 而不是 status()：status() 是纯读函数，bootstrap 和测试探针
        都会直接调它，不该有副作用。poll 才是「界面刷新」的入口。
        """
        if self._taskbar_hwnd is None:
            # 上色成功时 _titlebar_hwnd 已经有了，直接复用，省一次 EnumWindows
            self._taskbar_hwnd = self._titlebar_hwnd or titlebar.find_window(APP_TITLE)
            if not self._taskbar_hwnd:
                return              # 窗口还没建好，下一次 poll 再试
        hwnd = self._taskbar_hwnd
        try:
            if self.operation == 'record':
                flag = taskbar.TBPF_PAUSED if self.record_paused else taskbar.TBPF_INDETERMINATE
                taskbar.set_state(hwnd, flag)
            elif self.operation == 'export':
                # 千分比够了：任务栏那条进度条本身也就几十像素宽
                taskbar.set_progress(hwnd, int(self.export_fraction * 1000), 1000)
            else:
                taskbar.clear(hwnd)
        except Exception:
            logging.debug('Could not update the taskbar progress', exc_info=True)

    def poll(self, since=0, preview_rev=-1):
        self._sync_taskbar()
        lines = [entry for entry in self._log if entry['id'] > int(since or 0)]
        # 预览图只在**换过**的时候才回传。一张 400px 的 PNG base64 有几万字符，
        # 150ms 一次、一秒近七次地重发同一张图纯属浪费 —— 而这串东西要经过
        # 一次 JSON 序列化和 WebView 桥。前端把手里那版 rev 带上来，一样就不发。
        try:
            same_preview = int(preview_rev) == self._preview_rev
        except (TypeError, ValueError):
            same_preview = False
        return {
            'status': self.status(),
            'log': lines,
            'preview': None if same_preview else self.preview_data,
        }

    # ------------------------------------------------------------ SAI 探测
    async def _detect_loop(self):
        while not self._closing:
            try:
                if self.operation is None:
                    self.set_sai_proc()
                    self._maybe_retry_auto_detect()
            except Exception:
                logging.exception('SAI detection failed')
            await asyncio.sleep(0.4)

    def clear_sai_proc(self):
        if self.sai_proc is not None:
            try:
                self.sai_proc.close()
            except Exception:
                logging.debug('Could not close SAI handle', exc_info=True)
            self.sai_proc = None
            self.last_pid = None
        if self._sai_status != 'none':
            self._sai_status = 'none'
            self._sai_version = ''
            self._sai_override = False
        if self.canvases:
            self.canvases = []
            self._canvas_rev += 1
            self.preview_data = None
            self.preview_size = None
            self._preview_rev += 1

    def set_sai_proc(self):
        if self.sai_proc is not None and self.sai_proc.is_running():
            self.refresh_canvases()
            return
        self.clear_sai_proc()
        pid = sai.find_running_sai_pid()
        self.current_pid = pid
        if pid is None:
            return
        if pid == self.last_pid or time.time() < self._sai_retry_after:
            return
        try:
            self.last_pid = pid
            api = sai.get_sai_api_from_pid(pid)
            if api is not None:
                self.sai_proc = sai.SAI()
                self._sai_override = False
            else:
                # 未识别的版本，只能在同一代里挑覆盖目标：sai.exe 是 SAI1，
                # sai2.exe 是 SAI2。跨代套参数等于拿 32 位结构去读 64 位进程，
                # 画布永远读不出来，而界面照样显示「录制中」。
                # 2026-11-19 的真实案例：一个 64 位的 SAI2 (Preview.2019.01.29)
                # 被套上了 Ver.1.2.5 (32bit) 的参数 —— 因为 api_list 按类名排序，
                # SAIv1_* 恰好排在 SAIv2_* 前面，而默认索引是 0。
                try:
                    proc_name = (psutil.Process(pid).name() or '').lower()
                except Exception:
                    proc_name = ''
                wants_v1 = proc_name in ('sai.exe', 'sai')
                api_list = [a for a in sai.get_sai_api_list()
                            if issubclass(a, sai.SAIv1_API_Base) == wants_v1]
                if not api_list:
                    api_list = sai.get_sai_api_list()
                index = int(clamp_number(self.store.get('sai_version_override'), 0, 999, 0))
                index = min(index, len(api_list) - 1)
                self.sai_proc = sai.SAI(api_list[index])
                self._sai_override = True
                # 这条路以前是静默的：套上参数、界面显示「录制中」，录出来却是
                # 空帧或花屏，日志里一个错都没有 —— 排查成本极高。留一条明确的话，
                # 并把用户的 exe 指纹记下来，否则连做适配都无从下手。
                exe_hash = sai.get_exe_hash_by_pid(pid) or ''
                logging.warning(
                    '未识别的 SAI2 版本（exe MD5 %s）。已按「%s」的参数强行套用，'
                    '同时开始自动适配…',
                    exe_hash or '未知',
                    self.sai_proc.api.version_name)
                # 只对 64 位 sai2.exe 做自动探测。SAI1 是另一套结构，候选池里的
                # 偏移对它没有意义，扫一遍只会白耗 20 秒。
                if not wants_v1 and exe_hash:
                    self._schedule_auto_detect(pid, exe_hash)
            self._sai_status = 'running'
            self._sai_version = self.sai_proc.api.version_name
            self.refresh_canvases()
        except sai.NoSaiProcessDetected:
            # 进程在、但内存还读不了（刚启动 / 权限问题）。过两秒再试，
            # last_pid 已经在上面设过了，这里必须清掉，否则永远不会重试。
            self.last_pid = None
            self._sai_detect_failures += 1
            if self._sai_detect_failures <= 3:
                logging.warning('SAI process found but could not be read', exc_info=True)
            self._sai_retry_after = time.time() + 2.0

    # ------------------------------------------------ 未登记版本的自动探测
    def _schedule_auto_detect(self, pid:int, exe_hash:str):
        """排一次后台探测。

        每次 0.4 秒的检测循环都会走「未识别」分支，所以这里必须自己去重：
        同一个 pid + 同一个 MD5 在探测中就不重复起任务，失败后按 3s / 15s 退避，
        连续失败 3 次就放弃（再试也是白烧 CPU）。换了 pid 或换了 exe 才重新开始。
        """
        if self._closing or self.loop is None:
            return
        now = time.time()
        same = (pid == self._auto_detect_pid and exe_hash == self._auto_detect_hash)
        if same:
            task = self._auto_detect_task
            if task is not None and not task.done():
                return
            if self._auto_detect_failures >= 3 or now < self._auto_detect_next_try:
                return
        else:
            self._auto_detect_pid = pid
            self._auto_detect_hash = exe_hash
            self._auto_detect_failures = 0
            self._auto_detect_next_try = 0.0
        try:
            self._auto_detect_task = self.loop.create_task(
                self._auto_detect_worker(pid, exe_hash))
        except Exception:
            logging.exception('排自动探测任务失败')

    def _auto_detect_log(self, text:str):
        logging.info('[自动适配] %s', text)

    async def _auto_detect_worker(self, pid:int, exe_hash:str):
        """探测本体。

        **必须**丢进线程：它要读几 MB 内存、给每个候选真解一张图，最长 20 秒。
        放事件循环里跑，界面会整个冻住 —— 而这条循环正是刷新画布下拉的同一根线。
        """
        version_name = 'SAI Ver.2 (64bit) 未识别版本'
        try:
            res = await asyncio.to_thread(
                auto_detect.detect, pid, exe_hash, version_name,
                self._auto_detect_log, 20.0)
        except Exception:
            logging.exception('自动探测异常')
            res = {'ok': False, 'message': '自动探测出错了，请用 Sai2-Sniffer 反馈'}
        if res.get('ok'):
            self._apply_auto_detect(pid, res)
            return
        self._auto_detect_failures += 1
        # 第一次失败隔 3 秒再试（用户很可能正在开画布），之后拉长到 15 秒
        self._auto_detect_next_try = time.time() + (
            3.0 if self._auto_detect_failures <= 1 else 15.0)
        if self._auto_detect_failures == 1:
            logging.warning(res.get('message') or '自动适配没成功')

    def _apply_auto_detect(self, pid:int, res:dict):
        """探测成功：换掉正在用的 api，刷新画布。

        探测期间用户可能关掉 SAI2 重开，那这个结果就是给上一个进程的，
        直接丢弃 —— 套到新进程上会读出一堆垃圾。
        """
        if self.current_pid != pid:
            logging.info('自动适配完成，但 SAI2 已经换了一个进程，结果丢弃')
            return
        cls = res.get('cls')
        if cls is None:
            return
        try:
            proc = sai.SAI(cls, pid=pid)
        except sai.NoSaiProcessDetected:
            logging.warning('自动适配拿到参数了，但进程已经读不到')
            return
        old = self.sai_proc
        self.sai_proc = proc
        if old is not None and old is not proc:
            try:
                old.close()
            except Exception:
                logging.debug('关闭旧的 SAI 句柄失败', exc_info=True)
        self._sai_override = False
        self._sai_status = 'running'
        self._sai_version = cls.version_name
        logging.info(res.get('message') or '已自动适配当前 SAI2 版本')
        self.refresh_canvases()

    def _maybe_retry_auto_detect(self):
        """探测失败之后还愿不愿意再试。

        关键点：套参数成功之后 sai_proc 就不为 None 了，``set_sai_proc`` 下次
        会直接走「已经在跑，刷新画布」提前返回 —— 未识别分支只走一次。
        所以「用户后来才打开画布」这种情况，只能靠这里把它重新排上。
        """
        if not self._sai_override or self.canvases:
            return
        pid = self.current_pid
        if not pid or pid != self._auto_detect_pid or not self._auto_detect_hash:
            return
        if self._auto_detect_failures >= 3:
            return
        if time.time() < self._auto_detect_next_try:
            return
        self._schedule_auto_detect(pid, self._auto_detect_hash)

    def refresh_canvases(self):
        if self.sai_proc is None:
            return
        try:
            items = self.sai_proc.api.get_canvas_list()
        except Exception as exc:
            # 以前这里是 logging.debug —— 结果用户看到「画布下拉框是空的」，
            # 既不知道是没开画，也不知道是读内存失败，日志里同样什么都没有。
            # 2026-11-19 有 SAI1 用户就这样问过。现在至少记一条 warning。
            message = f'{type(exc).__name__}: {exc}'
            if message != self._canvas_error:
                self._canvas_error = message
                logging.warning('列画布失败：%s', message)
            return
        if self._canvas_error:
            self._canvas_error = ''
        names = [f'{c.get_name()} ({c.get_short_path()})' for c in items]
        if names != self.canvases:
            self.canvases = names
            self._canvas_rev += 1
            self.schedule_preview()

    def selected_canvas(self):
        """按存下来的显示名找画布；找不到就用第一个（画布可能已经关了）。"""
        if self.sai_proc is None:
            return None
        try:
            items = self.sai_proc.api.get_canvas_list()
        except Exception:
            return None
        if not items:
            return None
        saved = self.store.get('sai_canvas') or ''
        if saved in self.canvases:
            return items[self.canvases.index(saved)]
        return items[0]

    def schedule_preview(self):
        """重抓一次画布预览。

        **必须**走 ``run_coroutine_threadsafe``，不能直接 ``asyncio.ensure_future``：
        ``set_settings`` 是 pywebview 从 JS 线程直接调的，那个线程里没有运行中的
        事件循环，``ensure_future`` 会抛 ``RuntimeError: There is no current event
        loop in thread ...``，再被前端的 ``catch (e) {}`` 静默吞掉 —— 表现就是
        「切换画布后预览一直是旧的」，而且界面上一个错都不报。
        从 ``_detect_loop``（本来就在 loop 线程里）调也安全，这个 API 是线程安全的。
        """
        loop = getattr(self, 'loop', None)
        if loop is None or loop.is_closed():
            return
        task = self._preview_task
        if task is not None and not task.done():
            task.cancel()
        self._preview_task = self.submit(self._refresh_preview)

    async def _refresh_preview(self):
        # 每次刷新领一个序号。连续切画布时，先发的那次可能后完成（抓图快慢不一），
        # 没有序号的话它会把新画布的图盖掉 —— 看起来就像「切了没反应」。
        self._preview_seq += 1
        seq = self._preview_seq
        canvas = self.selected_canvas()
        if canvas is None or self.sai_proc is None:
            self.preview_data = None
            self.preview_size = None
            self._preview_rev += 1
            return

        def grab():
            api = self.sai_proc.api
            # 抓图尺寸跟着显示框走。原来写死 100px，而框有 196px 宽，等于把
            # 100px 的图放大两倍显示 —— 糊是必然的。400px 在 2x 屏上也够锐。
            map_level = api.get_map_level_for_size(canvas, PREVIEW_PX)
            image = api.get_canvas_image(canvas, map_level)
            # 真实画布像素从 canvas.width/height 读（上面刚 update_canvas 过）。
            # 不能拿缩放后的图去量：那张图已经被 thumbnail 到 400 以内，
            # 量出来是缩略图尺寸，标签就会把 4000px 的画布写成 400×300。
            size = (int(canvas.width), int(canvas.height))
            import cv2
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            from PIL import Image
            img = Image.fromarray(image)
            img.thumbnail((PREVIEW_PX, PREVIEW_PX))
            buf = io.BytesIO()
            # 画布线稿大片同色，PNG 在这种图上压得很好；JPEG 反而会在边缘糊出噪点
            img.save(buf, format='PNG', optimize=True)
            url = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')
            return url, size

        try:
            data, size = await asyncio.to_thread(grab)
        except Exception:
            logging.debug('Canvas preview failed', exc_info=True)
            data, size = None, None
        if seq != self._preview_seq:
            return                      # 期间又切了画布，这次结果作废
        self.preview_data = data
        self.preview_size = size
        self._preview_rev += 1

    # ------------------------------------------------------------ 导出预估
    async def _summary_loop(self):
        while not self._closing:
            await asyncio.sleep(0.5)
            if not self._summary_dirty:
                continue
            self._summary_dirty = False
            try:
                frames_path = timelapse.resolve_frames_dir(self.store.get('frames_file') or '')
                files, count, _size = await asyncio.to_thread(timelapse.scan_frames, frames_path)
                # 成片名也一起算进来：录制时记下的画布名（老会话没有就空）
                name = timelapse.read_session_meta(frames_path).get('name') or ''
            except Exception:
                count, name, files = 0, '', []
            self.summary_frames = int(count or 0)
            self.summary_segments = len(files or [])
            self.summary = self.format_summary(count, name)

    def output_container(self) -> str:
        """成片最终会用哪个容器。跟着「沿用录制格式」这个开关走。"""
        if self.store.get('export_user_recording'):
            return self.recording_format()[0]
        return self.export_format()[0]

    def format_summary(self, frame_count: int, output_name: str = '') -> str:
        if frame_count <= 0:
            return tr(MESSAGES['no_frames'])
        fps = clamp_number(self.store.get('export_fps'), 1, 120, 30)
        limit = clamp_number(self.store.get('export_time_limit'), 0, 86400, 0)
        source_seconds = frame_count / fps
        output_seconds = source_seconds if limit <= 0 else min(source_seconds, limit)
        text = tr(MESSAGES['frames_summary']).format(
            frame_count, format_duration(source_seconds), format_duration(output_seconds))
        if limit > 0 and source_seconds > limit + 0.5:
            text += ' ' + tr(MESSAGES['sampled'])
        if output_name:
            text += ' · ' + tr(MESSAGES['will_save_as']).format(
                f'{output_name}.{self.output_container()}')
        return text

    def touch_summary(self):
        self._summary_dirty = True

    # ------------------------------------------------------------ 标题栏
    def apply_titlebar(self):
        """给系统标题栏上色。DwmSetWindowAttribute 可以从任意线程调。

        返回是否成功。**失败要能看出来** —— 之前这里静默失败，
        界面上就是深色 UI 顶着一条白标题栏，还得靠截图人肉发现。
        """
        theme = self.store.get('theme') or 'sai-dark'
        bg, fg, border, dark = TITLEBAR_COLORS.get(theme, TITLEBAR_COLORS['sai-dark'])
        hwnd = titlebar.find_window(APP_TITLE)
        if not hwnd:
            logging.debug('Title bar: window %r not found yet', APP_TITLE)
            return False
        self._titlebar_hwnd = hwnd
        self._titlebar_mode = titlebar.apply(hwnd, bg, fg, border, dark)
        if self._titlebar_mode is None:
            logging.warning('Title bar: DWM refused to colour the caption')
            return False
        logging.debug('Title bar themed via %s', self._titlebar_mode)
        return True

    def apply_titlebar_when_ready(self, attempts=40, delay=0.1):
        """窗口刚创建时 FindWindowW 可能还找不到，重试到成功为止。"""
        for _ in range(attempts):
            if self.apply_titlebar():
                return True
            time.sleep(delay)
        return False

    # ------------------------------------------------------------ 窗口几何
    def save_geometry(self):
        """关窗前把位置大小写回配置，格式和 Tk 版一致。

        最大化时不覆盖 ``win_geometry`` —— 否则下次「还原」出来的还是最大化
        的尺寸，就再也回不到用户原来的小窗口了。
        """
        hwnd = self._titlebar_hwnd or titlebar.find_window(APP_TITLE)
        if not hwnd:
            return
        try:
            if titlebar.is_maximized(hwnd):
                self.store.update({'win_state': 'zoomed'})
                return
            left, top, right, bottom, _work_h = titlebar.window_metrics(hwnd)
            width, height = right - left, bottom - top
            if width < MIN_WIDTH // 2 or height < MIN_HEIGHT // 2:
                return          # 最小化/隐藏状态下量到的是怪数字，别写进去
            self.store.update({
                'win_state': 'normal',
                'win_geometry': f'{width}x{height}+{left}+{top}',
                'win_size': [width, height],
            })
        except Exception:
            logging.debug('Could not save window geometry', exc_info=True)

    # ------------------------------------------------------------ 录制 / 导出
    async def toggle_record(self):
        if self.operation is not None:
            self.cancel_operation()
            return
        await self._run_operation('record')

    async def toggle_export(self):
        if self.operation is not None:
            self.cancel_operation()
            return
        await self._run_operation('export')

    async def stop_operation(self):
        """明确停止当前操作（不是 toggle）。

        确认框用这个：从弹框到点确认之间隔着几秒，录制可能已经被热键停掉了，
        那时再 toggle 会反过来**开始**一段新录制。
        """
        if self.operation is not None:
            self.cancel_operation()

    def cancel_operation(self):
        if self.operation_task is not None:
            self.operation_task.cancel()

    async def pause_record(self):
        """暂停录制。只对正在录制的会话有效，重复调用无副作用。"""
        if self.operation != 'record' or self._pause_event is None or self.record_paused:
            return False
        self.record_paused = True
        self._pause_started = time.time()
        self._pause_event.set()
        logging.info(tr('Recording paused'))
        return True

    async def resume_record(self):
        """继续录制。累计的暂停时长要记下来，否则计时器会把暂停的时间也算进去。"""
        if self.operation != 'record' or self._pause_event is None or not self.record_paused:
            return False
        if self._pause_started:
            self._paused_total += time.time() - self._pause_started
            self._pause_started = 0.0
        self.record_paused = False
        self._pause_event.clear()
        logging.info(tr('Recording resumed'))
        return True

    async def _run_operation(self, mode):
        self.operation = mode
        # 记下**自己这个 task**，停止按钮才有东西可 cancel。
        # 以前这里没赋值，operation_task 永远是 None，于是 cancel_operation()
        # 是个空操作 —— 界面上按钮已经变成「停止录制」，点下去却什么都不发生。
        # 热键 Ctrl+Alt+S 走的是同一条路，所以那时也停不下来。
        self.operation_task = asyncio.current_task()
        self.record_frames = 0
        self.export_fraction = 0.0
        self.export_label = ''
        self.record_paused = False
        self._paused_total = 0.0
        self._pause_started = 0.0
        # 每次录制新建一个：上一个会话残留的置位状态不该带进来
        self._pause_event = asyncio.Event() if mode == 'record' else None
        started = time.time()
        try:
            if mode == 'record':
                logging.info(tr('Recording started'))
                self.record_started = started
                await self.record_sai()
            else:
                logging.info(tr('Exporting started'))
                await self.export()
        except asyncio.CancelledError:
            # 停止按钮 / 热键 / 关窗都从这里退出。日志统一由 finally 打，
            # 这里再打一条就成了「已停止录制」连着出现两遍。
            pass
        except Exception as ex:
            logging.exception(ex)
            self.last_error = str(ex).strip() or type(ex).__name__
            logging.error(self.last_error)
        finally:
            if mode == 'record':
                logging.info(tr('Recording stopped'))
                self.touch_summary()
            else:
                logging.info(tr('Exporting stopped'))
            self.operation = None
            self.record_started = 0.0
            self.operation_task = None
            # 暂停状态必须跟着会话一起清掉：残留的置位会让下一次录制一开
            # 就卡在 await paused.wait() 上，界面上显示「录制中」却一帧不录。
            self.record_paused = False
            self._pause_event = None
            self._paused_total = 0.0
            self._pause_started = 0.0

    def _pick_record_dir(self, canvas, image_size_limit: int) -> tuple[Path, str | None]:
        """决定这次录到哪个目录。返回 ``(目录, 被追加会话的起始时间或 None)``。

        原本每次录制都新建一个会话子目录，而导出时 `resolve_frames_dir` 只取
        最新那个 —— 所以「今天画一半、明天接着画」默认是**两段独立的视频**，
        旧那段还躺在磁盘上，但不会进成片。要接上只有主动复用同一个会话目录，
        这件事必须由用户明确选择，不能替他决定。
        """
        root = timelapse.expand_path(self.store.get('frames_file') or '')
        last = timelapse.resolve_frames_dir(root)
        is_session = last != root and bool(timelapse.SESSION_DIR_RE.match(last.name))

        if self.store.get('append_last_session') and is_session and last.is_dir():
            _files, count, size = timelapse.scan_frames(last)
            if count > 0:
                meta = timelapse.read_session_meta(last)
                logging.info(tr(MESSAGES['append_to_session']).format(count, last.name))
                self._warn_size_change(canvas, image_size_limit, size)
                return last, meta.get('started')
            # 目录在但一帧没有（上次刚开录就被关掉），直接复用它没有意义，
            # 新建一个更干净 —— 下面的分支会照常建新目录。

        new_dir = timelapse.make_session_dir(root)
        if is_session and last.is_dir():
            # 上次的帧还在，这次却另开一段。不说一句的话，用户导出时会以为
            # 「昨天画的丢了」，或者反过来以为已经接上了。
            logging.info(tr(MESSAGES['new_session_hint']).format(last.name))
        return new_dir, None

    def _warn_size_change(self, canvas, image_size_limit: int, prev_size) -> None:
        """接着录之前先比一下尺寸。

        导出时的目标尺寸取所有分片里最大的那个，尺寸不一样的那段会被
        `image_fit_and_pad` 等比缩放、四周补黑边。这种事在成片里一眼就能看出
        「后半段怎么缩水了」，但录制当时毫无征兆，所以提前说。
        """
        try:
            w, h = int(canvas.width), int(canvas.height)
            # get_fit_size 的语义是「缩放到填满目标框」，会把小画布**放大**；
            # 而录制走的是 image_thumbnail，只缩不放。照抄 get_fit_size 会让
            # 64×64 的会话被算成 1920×1920，同一张画布接续也报「尺寸不同」。
            if w <= image_size_limit and h <= image_size_limit:
                current = timelapse.Size(w, h)
            else:
                current = timelapse.get_fit_size(
                    timelapse.Size(w, h),
                    timelapse.Size(image_size_limit, image_size_limit))
            current = timelapse.even_size(current)
        except Exception:
            logging.debug('Could not compare canvas sizes', exc_info=True)
            return
        if prev_size is None or getattr(prev_size, 'width', 0) <= 0:
            return
        if (abs(current.width - prev_size.width) > 2
                or abs(current.height - prev_size.height) > 2):
            logging.warning(tr(MESSAGES['append_size_mismatch']).format(
                current.width, current.height,
                prev_size.width, prev_size.height))

    def _session_meta(self, canvas) -> dict:
        """这次录制对应的画布信息，写进会话目录给导出用。

        画布名就是 SAI 里的文件名（``正比30.sai2``），未保存的画布叫
        「新建画布1」。读指针偶尔会因为画布刚被关掉而失败，那就只留时间戳，
        不能让一个命名功能把录制搞挂。
        """
        meta = {'started': datetime.now().isoformat(timespec='seconds')}
        try:
            meta['canvas'] = canvas.get_name()
            meta['canvas_path'] = canvas.get_short_path()
        except Exception:
            logging.debug('Could not read canvas name', exc_info=True)
        # 存的是处理过的名字：导出时直接拿来当文件名，不必再考虑非法字符
        meta['name'] = timelapse.safe_filename(meta.get('canvas', ''))
        return meta

    async def record_sai(self):
        if self.sai_proc is None:
            logging.info(tr(MESSAGES['no_sai']))
            return
        # 没设保存位置就开录的话，make_session_dir('') 会把 Path('.') 当根目录，
        # 帧直接写进程序当前目录 —— 发布版可能装在 Program Files 里，直接失败。
        if not self.save_root():
            logging.warning(tr(MESSAGES['no_save_dir']))
            return
        with sai.SAI(type(self.sai_proc.api)) as sai_proc:
            canvases = sai_proc.api.get_canvas_list()
            if not canvases:
                logging.info(tr(MESSAGES['no_canvases']))
                return
            canvas = self.selected_canvas() or canvases[0]
            # 画布宽高读成 0 说明这个 SAI 版本的字段偏移不对。不在这里拦，录制会
            # 一路跑到建 VideoWriter 才失败，而且报的是「不支持编码器 avc1」——
            # 用户被这句话带偏去找编码器（2026-11-19 的真实案例：未识别的
            # 2024.11.23 构建被套上 2025.02.10 的参数，宽高读出 0x0）。
            cw, ch = int(canvas.width), int(canvas.height)
            if cw <= 0 or ch <= 0:
                logging.error(
                    '画布尺寸读成 %dx%d —— 这个 SAI2 版本的参数不对'
                    '（当前套用的是「%s」）。请运行 Sai2-Sniffer 生成报告发回来。',
                    cw, ch, self._sai_version or '未知')
                return
            image_size_limit = int(clamp_number(self.store.get('image_size_limit'), 100, 16384, 1920))
            frames_path, append_from = self._pick_record_dir(canvas, image_size_limit)
            # 记下这次录的是哪个画布：目录名是时间戳，过两天再看根本认不出
            # 画的是哪张。导出时拿这份记录给成片命名。
            meta = self._session_meta(canvas)
            if append_from:
                # 复用会话时保留最初的开始时间 —— 这段录制跨了两天，但它是一段，
                # 成片名和「从什么时候开始」都该按第一次算。
                meta['started'] = append_from
            timelapse.write_session_meta(frames_path, meta)
            logging.info(tr('Recording session:') + f' {frames_path}')
            container, codec = self.recording_format()
            auto_split = int(clamp_number(self.store.get('auto_split_sai'), 0, 100000, 0))

            def on_progress(frames):
                self.record_frames = frames

            reason = await timelapse.sai_capture(
                sai_proc, canvas, image_size_limit, frames_path, container, codec, auto_split,
                on_progress=on_progress, paused=self._pause_event,
            )
            if reason == 'canvas_lost':
                logging.warning(tr('Recording stopped: the SAI canvas was closed'))
            elif reason == 'no_window':
                logging.warning(tr('Recording stopped: could not find the SAI window'))

    def _format_of(self, prefix):
        types = [('mp4', 'avc1'), ('webm', 'vp80'), None]
        index = int(clamp_number(self.store.get(f'{prefix}_type'), 0, len(types) - 1, 0))
        video_format = types[index]
        if video_format is None:
            container = self.store.get(f'{prefix}_custom_container') or 'mp4'
            codec = self.store.get(f'{prefix}_custom_codec') or 'avc1'
            return container, codec
        return video_format

    def recording_format(self):
        return self._format_of('recording')

    def export_format(self):
        return self._format_of('export')

    def save_root(self) -> str:
        """用户选的保存位置。空字符串表示还没选过（首次启动）。"""
        return (self.store.get('frames_file') or '').strip()

    def use_default_save_dir(self) -> str:
        """首次引导里点「就用这里」：建目录并设成保存位置。"""
        target = default_save_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            logging.exception('Could not create the default save folder')
            return ''
        self.store.set('frames_file', str(target))
        self.touch_summary()
        logging.info(tr(MESSAGES['frames_path']) + f': {target}')
        return str(target)

    async def export(self):
        if not self.save_root():
            logging.warning(tr(MESSAGES['no_save_dir']))
            return
        frames_path = timelapse.resolve_frames_dir(self.store.get('frames_file') or '')
        logging.info(tr('Using recording session:') + f' {frames_path}')
        export_time_limit = clamp_number(self.store.get('export_time_limit'), 0, 86400, 0)
        preview_last = bool(self.store.get('export_preview_last_frame'))
        preview_duration = clamp_number(self.store.get('export_preview_duration'), 0, 60, 0)
        fps = clamp_number(self.store.get('export_fps'), 1, 120, 30)
        if self.store.get('export_user_recording'):
            container, codec = self.recording_format()
        else:
            container, codec = self.export_format()
        # 只有一个目录：成片落在会话子目录旁边。以前还支持单独配一个导出目录，
        # 结果界面上两个框、用户得猜哪个管什么，索性合并。
        output_path = ''
        # 录制时记下的画布名 → 成片名。老版本录的会话没有这份记录，
        # 退回用时间戳目录名。
        output_name = timelapse.read_session_meta(frames_path).get('name') or ''

        # 进度条从 0 开始，别让上一次的残留值先闪一下；
        # 成片卡片也先收起来，等这次真的写完了再出现
        self.export_fraction = 0.0
        self.export_label = ''
        self.export_info = {}
        state = {'done': 0, 'total': 1}
        # 缩略图取写入的最后一帧。只留引用不拷贝 —— 4K 一帧 36MB，
        # 导出结束后这张图就没人用了，没必要为了它多占一份内存。
        last = {'frame': None}

        def progress_iter(iterable, total, unit='it'):
            state['total'] = max(int(total or 1), 1)
            for index, item in enumerate(iterable):
                state['done'] = index + 1
                self.export_fraction = state['done'] / state['total']
                self.export_label = f'{state["done"]} / {state["total"]}'
                last['frame'] = item
                yield item

        def run():
            return timelapse.export(progress_iter, export_time_limit, preview_last,
                                    preview_duration, fps, frames_path, container, codec,
                                    output_path, output_name)

        saved = await asyncio.to_thread(run)
        if saved:
            self.export_result = str(saved)
            # 读文件大小和回读帧数都要碰磁盘，别卡事件循环
            self.export_info = await asyncio.to_thread(
                self._build_export_info, saved, fps, last['frame'])
            logging.info(tr('Saved to') + f' {saved}')

    # ------------------------------------------------------------ 成片卡片
    @staticmethod
    def _thumbnail_data_url(frame, max_side=200) -> str:
        """把一帧压成小 JPEG 的 data URL，给成片卡片当缩略图。

        用 JPEG 不用 PNG：200px 的缩略图 PNG 往往更大，而这里也不需要透明度。
        """
        if frame is None:
            return ''
        try:
            import cv2
            height, width = frame.shape[:2]
            if not height or not width:
                return ''
            scale = max_side / max(height, width)
            if scale < 1:
                frame = cv2.resize(
                    frame, (max(1, int(width * scale)), max(1, int(height * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not ok:
                return ''
            return 'data:image/jpeg;base64,' + base64.b64encode(buf.tobytes()).decode('ascii')
        except Exception:
            logging.debug('Could not build the export thumbnail', exc_info=True)
            return ''

    def _build_export_info(self, saved: Path, fps, frame) -> dict:
        """成片卡片要展示的数据。任何一项读不出来就留空，不让整张卡片挂掉。"""
        info = {
            'path': str(saved),
            'name': saved.name,
            'size': 0,
            'frames': 0,
            'duration': 0.0,
            'thumb': self._thumbnail_data_url(frame),
        }
        try:
            info['size'] = saved.stat().st_size
        except OSError:
            logging.debug('Could not stat the exported file', exc_info=True)
        try:
            # 回读成片拿真实帧数 —— 比用导出时的计数准：预览帧、跳帧都算进去了
            with timelapse.VideoReader(saved) as reader:
                count = reader.get_frame_count()
            info['frames'] = count
            if fps:
                info['duration'] = count / fps
        except Exception:
            logging.debug('Could not read back the exported video', exc_info=True)
        return info

    # ------------------------------------------------------------ 归档
    def archive_frames(self) -> dict:
        """把当前会话的帧收进 ``archive/``，让下一次导出从零开始。

        导出读的是「最新的会话目录」（``resolve_frames_dir``），所以只要把刚导完
        的那个会话移出根目录，它就不会再被写进下一支视频。文件仍在磁盘上、
        目录名不变，想找回就手动挪回去 —— 全程不删任何东西。
        """
        root = timelapse.expand_path(self.store.get('frames_file') or '')
        session = timelapse.resolve_frames_dir(root)
        if session == root or not session.is_dir():
            # 平铺目录是用户自己的布局（老数据），不归这个功能动
            return {'moved': 0, 'session': '', 'reason': 'flat'}
        files = [f for f in session.iterdir()
                 if f.is_file() and not f.name.startswith('.')]
        if not files:
            return {'moved': 0, 'session': session.name, 'reason': 'empty'}

        target = root / 'archive' / session.name
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logging.warning('Could not create the archive folder: %s', exc)
            return {'moved': 0, 'session': session.name, 'reason': str(exc)}

        moved, failed = 0, 0
        for f in files:
            try:
                shutil.move(str(f), str(target / f.name))
                moved += 1
            except Exception as exc:
                failed += 1
                logging.warning('Could not archive %s: %s', f.name, exc)
        try:
            session.rmdir()          # 还有移动失败的文件时会抛，那就留着
        except OSError:
            pass
        if moved:
            logging.info(tr(MESSAGES['archived_frames']).format(moved, session.name))
            self.touch_summary()
        return {'moved': moved, 'session': session.name,
                'target': str(target), 'failed': failed}

    # ------------------------------------------------------------ 设置
    def set_settings(self, patch: dict):
        """前端改一个字段就整体推过来，这里只落变化的那些。"""
        changed = {}
        for key, value in (patch or {}).items():
            if key not in DEFAULTS:
                continue
            if self.store.get(key) != value:
                changed[key] = value
        if not changed:
            return self.status()
        self.store.update(changed)
        if 'frames_file' in changed or 'export_fps' in changed or 'export_time_limit' in changed:
            self.touch_summary()
        if 'sai_canvas' in changed:
            self.schedule_preview()
        if 'lang' in changed:
            set_locale(self.store.get('lang') or 'zh_CN')
            self.settings_rev += 1
            self.touch_summary()
        if 'theme' in changed:
            self.apply_titlebar()
        if 'floatbar' in changed:
            self._sync_floatbar()
        return self.status()

    def restore_defaults(self):
        keep = {'frames_file', 'theme', 'lang', 'win_geometry', 'win_state'}
        restored = self.store.reset(keep=keep)
        logging.info(tr(MESSAGES['restored_n']).format(len(restored)))
        self.settings_rev += 1
        self.touch_summary()
        return self.store.all()

    # ------------------------------------------------------------ 快照
    def bootstrap(self):
        return {
            'title': APP_TITLE,
            'version': get_version(),
            'strings': self.strings(),
            'settings': self.store.all(),
            'status': self.status(),
            'log': [entry for entry in self._log],
            'preview': self.preview_data,
            'log_path': str(get_log_path()) if get_log_path() else '',
            'video_types': [tr(MESSAGES['video_mp4']), tr(MESSAGES['video_webm']), tr(MESSAGES['video_custom'])],
            'sai_versions': [api.version_name for api in sai.get_sai_api_list()],
            # 首次启动的引导要先把建议路径摆出来，免得用户对着空框发呆
            'default_save_dir': str(default_save_dir()),
        }


def default_save_dir() -> Path:
    """首次启动时建议的保存位置。

    放在用户的「视频」文件夹下面：Windows 上那是大家已经习惯存视频的地方，
    中文系统里显示成「视频」但真实路径仍然是 ``Videos``，不会有编码问题。
    """
    return Path.home() / 'Videos' / APP_NAME


def get_version() -> str:
    """界面上显示的版本号（前端会加 v 前缀）。

    以前走 importlib.metadata，读不到就回落成硬编码的 '3.17.0' —— 两处版本号
    必然对不上。现在只认包根那一个常量。
    """
    return __version__


GEOMETRY_RE = re.compile(r'^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$')


def parse_geometry(text: str, fallback=DEFAULT_SIZE):
    """把 Tk 存的 ``'911x750+523+121'`` 解析成 ``(w, h, x, y)``。

    配置文件和 Tk 版共用，所以必须认 Tk 的格式 —— 包括负偏移
    （``'911x750-100+50'``，从屏幕右边算）。坐标落在屏幕外（拔了外接屏）
    时只保留尺寸，让窗口自己居中。
    """
    width, height = fallback
    match = GEOMETRY_RE.match((text or '').strip())
    if not match:
        return width, height, None, None
    try:
        width, height = int(match.group(1)), int(match.group(2))
        x, y = int(match.group(3)), int(match.group(4))
    except (TypeError, ValueError):
        return width, height, None, None
    width = max(MIN_WIDTH, width)
    height = max(MIN_HEIGHT, height)
    # 至少留 120x60 在屏幕上，否则用户拖不回来
    screen_w = ctypes.windll.user32.GetSystemMetrics(0) if os.name == 'nt' else 1920
    screen_h = ctypes.windll.user32.GetSystemMetrics(1) if os.name == 'nt' else 1080
    # 尺寸也别超过工作区：窗口尺寸固定之后，从 1080p 换到 768 高的笔记本时，
    # 存下来的 820 会顶到任务栏下面去。这里夹一次，界面那边会自动退回滚动。
    if os.name == 'nt':
        try:
            from ctypes import wintypes
            work = wintypes.RECT()
            # SPI_GETWORKAREA = 0x0030，已扣掉任务栏
            if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(work), 0):
                work_w = work.right - work.left
                work_h = work.bottom - work.top
                if work_w > 0 and work_h > 0:
                    width = min(width, max(MIN_WIDTH, work_w))
                    height = min(height, max(MIN_HEIGHT, work_h - 8))
        except Exception:
            logging.debug('Could not read the work area', exc_info=True)
    if not (-width + 120 < x < screen_w - 120 and 0 <= y < screen_h - 60):
        return width, height, None, None
    return width, height, x, y


class Api:
    """暴露给 JS 的接口。方法名会被 js_api 原样映射成 window.pywebview.api.xxx。

    注意属性名必须带下划线：pywebview 生成 JS 桥时会对 js_api 对象做
    ``dir()`` + ``getattr()`` 的**递归**遍历（见 webview/util.py: get_functions），
    遇到非 callable 且带 ``__module__`` 的属性就往里钻。以前这里叫 ``self.backend``，
    于是它顺着 ``backend.window.native.AccessibilityObject.Bounds.Empty.Empty...``
    一路走进 .NET 对象图，递归到爆栈，日志里每隔两秒刷一遍
    ``maximum recursion depth exceeded``，启动也白等两秒。
    下划线开头的属性会被 get_functions 直接跳过，这就是官方留的逃生口。
    """

    def __init__(self, backend: Backend):
        self._backend = backend

    def bootstrap(self):
        return self._backend.bootstrap()

    def poll(self, since=0, preview_rev=-1):
        return self._backend.poll(since, preview_rev)

    def set_settings(self, patch):
        return self._backend.set_settings(patch)

    def use_default_save_dir(self):
        return self._backend.use_default_save_dir()

    def toggle_record(self):
        self._backend.submit(self._backend.toggle_record)
        return True

    def pause_record(self):
        self._backend.submit(self._backend.pause_record)
        return True

    def resume_record(self):
        self._backend.submit(self._backend.resume_record)
        return True

    def stop_operation(self):
        self._backend.submit(self._backend.stop_operation)
        return True

    def archive_frames(self):
        """把刚导完的那批帧收进 archive/。同步执行 —— 只是一堆 move，很快。"""
        return self._backend.archive_frames()

    def toggle_export(self):
        self._backend.submit(self._backend.toggle_export)
        return True

    def pick_folder(self, current=''):
        path = filedialog.pick_folder(tr(MESSAGES['select']), current or '')
        if path:
            self._backend.set_settings({'frames_file': path})
        return path or ''

    def open_folder(self, path):
        try:
            target = Path(path)
            if not target.exists():
                return tr(MESSAGES['folder_missing']).format(path)
            os.startfile(str(target))
            return ''
        except Exception as ex:
            logging.exception('Could not open folder')
            return str(ex)

    def clear_log(self):
        self._backend.clear_log()
        return True

    def open_log_file(self):
        log_path = get_log_path()
        if log_path and Path(log_path).exists():
            os.startfile(str(Path(log_path).parent))
            return True
        return False

    def copy_log(self):
        text = '\n'.join(entry['text'] for entry in self._backend._log if entry['text'])
        return text

    def restore_defaults(self):
        return self._backend.restore_defaults()

    # 这里原来还有 window_height / set_window_height 两个接口，给「展开高级设置
    # 时把窗口撑高」用。窗口尺寸改成固定之后它们没有调用方了，一起删掉 ——
    # 留着一个没人调的 resize 入口，下次很容易又被接回去。


def _tame_chromium():
    """关掉 Chromium 在窗口拖动/缩放时会主动降帧的几个特性。

    实测过：拖动时页面 rAF 是满帧的、UI 线程响应中位 0.1ms，说明卡的不是
    我们的代码，而是 Chromium 自己的呈现策略。它默认会做两件对本程序有害的事：

    * ``CalculateNativeWinOcclusion`` —— 窗口每移动一次就重新判定"是否被别的
      窗口遮住"，判定期间暂停渲染。拖动时遮挡状态一直在变，渲染跟着反复启停，
      看起来就是一顿一顿的。桌面应用本来就不需要这个优化。
    * ``--disable-renderer-backgrounding`` / ``--disable-backgrounding-occluded-windows``
      —— 同理，窗口一旦被判定成"后台"或"被遮挡"就把渲染降到 1fps 甚至停掉。

    走环境变量而不是参数：pywebview 没暴露 browser args，而 WebView2 会读这个
    变量，且必须在创建 WebView2 环境之前设好（``webview.start`` 里才创建）。
    用 ``setdefault`` 是为了尊重用户已经设过的值。
    """
    if os.name != 'nt':
        return
    os.environ.setdefault(
        'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS',
        '--disable-features=CalculateNativeWinOcclusion'
        ' --disable-renderer-backgrounding'
        ' --disable-backgrounding-occluded-windows',
    )


def _apply_ui_env() -> None:
    """处理界面相关的环境变量 —— 给 WebView2 渲染出问题的用户一条自救路。

    2026-11-19 有人发截图：窗口里输入框、按钮、圆角都在，但**所有文字都不见了**。
    同版本的包在我这儿（源码版和打包版、深色浅色都试过）显示正常，所以是那台
    机器的渲染链路有问题：布局引擎还在跑，只有文字那一层没输出，典型是 GPU
    合成失败。

    兜两个开关，配一个 bat 就是「安全模式」：

        ART_TIMELAPSE_NO_GPU=1   禁用 WebView2 的 GPU 加速
        ART_TIMELAPSE_UI=tk      整个绕开 WebView2，走 Tk 备用界面
    """
    if os.environ.get('ART_TIMELAPSE_NO_GPU'):
        # pywebview 的 edgechromium 后端把这条环境变量透传给 WebView2
        os.environ.setdefault('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', '--disable-gpu')
        logging.info(tr('GPU acceleration disabled by ART_TIMELAPSE_NO_GPU'))


def force_tk_ui() -> bool:
    """ART_TIMELAPSE_UI=tk 时强制用 Tk 界面。"""
    return (os.environ.get('ART_TIMELAPSE_UI') or '').strip().lower() == 'tk'


def main():
    """入口。WebView2 起不来就退回 Tk 界面 —— 宁可难看，也不能打不开。"""
    _tame_chromium()
    _apply_ui_env()
    # 外部版本表必须在 Backend() 之前载入 —— 它在初始化时就会去认 SAI 进程的
    # 版本号，那时候表得是完整的。坏文件只会记一条 warning，不影响启动。
    extra = sai.load_external_versions()
    if extra:
        logging.info('外部版本表：载入 %d 条，现支持 %d 个版本',
                     extra, len(sai.sai_api_lookup))

    if force_tk_ui():
        logging.info(tr('Tk UI forced by ART_TIMELAPSE_UI=tk'))
        from . import gui
        gui.main()
        return

    backend = Backend()
    backend.start()

    width, height, x, y = parse_geometry(backend.store.get('win_geometry') or '')
    ui_dir = Path(__file__).resolve().parent / 'webui'
    kwargs = dict(
        js_api=Api(backend),
        width=width,
        height=height,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        background_color='#111316',
        text_select=False,
    )
    if x is not None and y is not None:
        kwargs.update(x=x, y=y)      # 不给 x/y 时 pywebview 会自己居中
    window = webview.create_window(APP_TITLE, url=str(ui_dir / 'index.html'), **kwargs)
    backend.window = window

    def on_closing():
        backend.save_geometry()
        backend.shutdown()

    window.events.closing += on_closing

    try:
        # args 必须是元组：pywebview 会把它展开传给 func
        webview.start(_post_start, (backend,),
                      debug=bool(os.environ.get('ART_TIMELAPSE_DEBUG')))
    except Exception:
        logging.exception('WebView failed to start, falling back to the Tk UI')
        from . import gui
        gui.main()


def _post_start(backend):
    """窗口起来之后补几件事：标题栏上色、恢复最大化、悬浮条、跑截图/自检脚本。"""
    backend.apply_titlebar_when_ready()

    # 悬浮条放这里而不是 main()：进程的 DPI 感知是 WebView2 建立时才设好的，
    # 更早起 Tk 会按 96 DPI 算尺寸，在高缩放比的屏幕上就是一条小砖。
    backend._sync_floatbar()

    if (backend.store.get('win_state') or '') == 'zoomed' and backend.window is not None:
        try:
            backend.window.maximize()
        except Exception:
            logging.debug('Could not maximise the window', exc_info=True)

    # 截图/自检用：ART_TIMELAPSE_UI_SCRIPT 里放一段 JS，起来后执行一次。
    # 顺带也验证了 evaluate_js 能不能从非主线程调用。
    script = os.environ.get('ART_TIMELAPSE_UI_SCRIPT')
    if script and backend.window is not None:
        try:
            # 必须等 bootstrap 真的跑完再执行：``window.__app`` 是 bootstrap 末尾才挂上的，
            # 而 ``window.pywebview.api`` 是 pywebview 在 DOM ready 后注入的。
            # 以前只等 ``__app``（脚本一解析就有），结果脚本在 api 还没注入时就跑了，
            # 里面的 ``api().xxx()`` 直接 TypeError，看起来像"功能没实现"。
            ready = ('typeof window.__app !== "undefined"'
                     ' && !!(window.pywebview && window.pywebview.api)')
            for _ in range(80):
                if backend.window.evaluate_js(ready):
                    break
                time.sleep(0.1)
            # 脚本的返回值也记一笔：探针要往回传测量值，而 document.title
            # 在 pywebview 里不会同步到窗口标题，没有别的回传通道。
            # 走 stderr 而不是 logging —— 日志区是给用户看的，不该出现调试输出；
            # 打包版 sys.stderr 是 None，print 会静默返回，不会炸。
            result = backend.window.evaluate_js(script)
            print(f'[ui-script] {result!r}', file=sys.stderr, flush=True)
        except Exception:
            logging.exception('UI script failed')
