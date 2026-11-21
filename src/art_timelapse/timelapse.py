# ---------------------------------------------------------------------------
# Modified from art-timelapse -- https://github.com/cromachina/art-timelapse
# Copyright (C) 2024-2026 cromachina. Licensed under GPL-3.0 (see LICENSE).
#
# 本文件在上游基础上做过修改：新增重绘稳态等待 REDRAW_SETTLE_SEC、0 帧会话目录清理、输出尺寸校验与编码器协商失败的详细报错
# 修改自 2026-11-19 起，逐次记录见 适配说明.md。
# ---------------------------------------------------------------------------
from pathlib import Path
from datetime import datetime
import asyncio
import json
import os
import re
import shutil
import tempfile
import time
import tkinter as tk
import logging
import sys

from PIL import Image, ImageTk
import cv2
import numpy as np
import pynput
import pywinctl
from pywinbox import Point, Size, Rect
from mss import mss

from . import asynctk, sai, _
# 版本号只在包根定义一次。以前这里是 importlib.metadata.version('art-timelapse')，
# 打包之后元数据不一定被收进去，读不到就抛异常；改成一个常量，冻结与否都一样。
from . import __version__

FPS = 30

# MSS has issues being created and destroyed multiple times on Linux.
sct = mss()

def get_size(img:np.ndarray) -> Size:
    return Size(img.shape[1], img.shape[0])

def get_channels(img:np.ndarray) -> int:
    return img.shape[2]

def to_shape(size:Size, channels) -> tuple[int, int, int]:
    return size.height, size.width, channels

def grab(sct, bbox) -> Image.Image:
    img = sct.grab(bbox)
    return Image.frombytes('RGB', img.size, img.bgra, 'raw', 'BGRX')

def get_fit_size(input_size:Size, output_size:Size) -> Size:
    output_size = output_size
    result_w = output_size.width
    result_h = int(output_size.width / input_size.width * input_size.height)
    if result_h > output_size.height:
        result_h = output_size.height
        result_w = int(output_size.height / input_size.height * input_size.width)
    return Size(result_w, result_h)

def get_reuse_array(storage:dict, key:str, shape:tuple) -> np.ndarray:
    if storage is None:
        return None
    if key in storage:
        array = storage[key]
        if array.shape != shape:
            array = np.empty(shape, dtype=np.uint8)
            storage[key] = array
        return array
    else:
        array = np.empty(shape, dtype=np.uint8)
        storage[key] = array
        return array

def image_fit_and_pad(image:np.ndarray, size:Size, interpolation=cv2.INTER_AREA, reuse_arrays:dict=None) -> np.ndarray:
    image_size = get_size(image)
    if image_size == size:
        return image
    fit_size = get_fit_size(image_size, size)
    fit_top = (size.height - fit_size.height) // 2
    fit_bottom = size.height - fit_size.height - fit_top
    fit_left = (size.width - fit_size.width) // 2
    fit_right = size.width - fit_size.width - fit_left
    if fit_size == image_size:
        resize_dst = image
    else:
        resize_dst = get_reuse_array(reuse_arrays, 'resize', to_shape(fit_size, get_channels(image)))
        resize_dst = cv2.resize(image, fit_size, resize_dst, interpolation=interpolation)
    border_dst = get_reuse_array(reuse_arrays, 'border', to_shape(size, get_channels(image)))
    return cv2.copyMakeBorder(resize_dst, fit_top, fit_bottom, fit_left, fit_right, cv2.BORDER_CONSTANT, border_dst)

def image_thumbnail(image:np.ndarray, size:Size, dst=None, interpolation=cv2.INTER_AREA):
    image_size = get_size(image)
    if image_size.width <= size.width and image_size.height <= size.height:
        return image
    fit_size = get_fit_size(image_size, size)
    if dst is not None and get_size(dst) != fit_size:
        dst = None
    return cv2.resize(image, fit_size, dst=dst, interpolation=interpolation)

def is_image_equal(a:np.ndarray | None, b:np.ndarray | None) -> bool:
    return (a is not None and b is not None) and (a.shape == b.shape) and (np.all(a == b))

def expand_path(path:Path) -> Path:
    path = Path(path)
    try:
        path = path.expanduser()
    except:
        pass
    return path

SESSION_DIR_RE = re.compile(r'^\d{8}_\d{6}(?:_\d+)?$')

def make_session_dir(frames_path:Path) -> Path:
    """为本次录制生成一个独立子目录（不创建，由 VideoSequenceWriter 建）。

    目录名是 `YYYYmmdd_HHMMSS`，字典序即时间序，方便导出时取最新。
    同一秒内重复开始会加 `_1`、`_2` 后缀。
    """
    root = expand_path(frames_path)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    session = root / stamp
    n = 1
    while session.exists():
        session = root / f'{stamp}_{n}'
        n += 1
    return session

SESSION_META = 'session.json'

# 鼠标松开到真正读画布之间等多久。用户在 SAI 里点「图层可见性」这类开关时，
# SAI 会立刻重绘整幅画布，而点击事件在松开那一刻就到我们手里 —— 在这个窗口
# 里读内存，拿到的是一半新一半旧的 tile，导出的成片里就是整幅横向红条
# （2026-11-19 用户报的「闪杆」，图里每隔十几像素一条）。100ms 覆盖一般画布的
# 重绘耗时；期间积压的事件会被 drain 掉，不会重复抓同一张图。
REDRAW_SETTLE_SEC = 0.15

# 文件名里不能出现的字符（Windows 一套），外加控制字符
_INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

def safe_filename(name:str, limit:int=80) -> str:
    """把画布名变成能当文件名用的东西。

    SAI 的画布名本身就是文件名（``正比30.sai2``），直接拿来命名最符合直觉。
    但要挡三种情况：未保存画布叫「新建画布1」还算能用的名字；用户手打的名字
    可能带 ``/ : ? *`` 这类非法字符；名字可能长得离谱。

    只做最小必要处理 —— 用户自己起的名字才是他要找的东西，别自作主张加前缀
    后缀、也别把中文转拼音。
    """
    raw = str(name or '').strip()
    # 先替换非法字符再取主干：`Path('a/b.sai2')` 会把 `a/` 当成目录丢掉，
    # 名字里带斜杠的画布会莫名其妙少一截。
    cleaned = _INVALID_NAME_CHARS.sub('_', raw).strip()
    stem = Path(cleaned).stem or cleaned
    stem = _INVALID_NAME_CHARS.sub('_', stem).strip(' .')
    return stem[:limit].strip(' .')

def write_session_meta(session:Path, meta:dict) -> None:
    """把「这次录制画的是哪个文件」记进会话目录。

    目录名是时间戳，过两天再看根本认不出画的是哪张。导出时靠这份记录给成片
    命名。写失败不影响录制本身，所以异常吞掉 —— 宁可名字退回时间戳，
    也不能因为一个 json 写不进去就录不了。
    """
    try:
        session = expand_path(session)
        session.mkdir(parents=True, exist_ok=True)
        (session / SESSION_META).write_text(
            json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    except Exception:
        logging.debug('Could not write session meta', exc_info=True)

def read_session_meta(session:Path) -> dict:
    """读回会话记录。没有（老版本录的）或坏了都返回空字典。"""
    try:
        raw = (expand_path(session) / SESSION_META).read_text(encoding='utf-8')
        meta = json.loads(raw)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}

def resolve_frames_dir(frames_path:Path) -> Path:
    """解析导出要读的目录。

    - 目录下有本工具建的会话子目录（`YYYYmmdd_HHMMSS`）→ 取最新的那次录制
    - 否则原样返回：平铺的旧数据，或用户已经把路径指到了某个子目录

    只认会话命名的子目录，避免误选用户自己的备份目录。
    """
    root = expand_path(frames_path)
    if not root.is_dir():
        return root
    subs = sorted(
        (e for e in root.iterdir() if e.is_dir() and SESSION_DIR_RE.match(e.name)),
        key=lambda p: p.name,
    )
    return subs[-1] if subs else root

def even_dim(a:int) -> int:
    return a if a % 2 == 0 else a + 1

def even_size(size:Size) -> Size:
    return Size(even_dim(int(size.width)), even_dim(int(size.height)))

def max_size(size_a:Size, size_b:Size) -> Size:
    return Size(max(size_a.width, size_b.width), max(size_a.height, size_b.height))

def point_in_bbox(bbox:Rect, point:Point):
    return bbox.left <= point.x <= bbox.right and bbox.bottom <= point.y <= bbox.top

def nth_counter(nth:int):
    counter = 1
    while True:
        if counter == 1:
            counter = nth
            yield True
        else:
            counter -= 1
            yield False

def get_spanned_rect(pos1:Point, pos2:Point) -> Rect:
    p1x, p1y = pos1
    p2x, p2y = pos2
    return Rect(min(p1x, p2x), min(p1y, p2y), max(p1x, p2x), max(p1y, p2y))

class WindowGrabber(tk.Toplevel):
    def __init__(self, drag_area, *args, **kwargs):
        if tk._default_root is None:
            self.root = asynctk.AsyncTk()
            self.root.withdraw()
            self.root_task = asyncio.create_task(self.root.async_main_loop())
        else:
            self.root = None
        self.event = asyncio.Event()
        super().__init__(*args, **kwargs)
        if drag_area:
            logging.info(_('Click and drag on a subwindow to start tracking an area. Right click to cancel.'))
        else:
            logging.info(_('Click on a subwindow to start tracking. Right click to cancel.'))

        self.drag_area = drag_area
        self.overrideredirect(True)
        mon = sct.monitors[0]
        self.sw = mon['width']
        self.sh = mon['height']
        self.geometry(f'{self.sw}x{self.sh}+0+0')
        self.attributes('-topmost', True)
        self.canvas = tk.Canvas(self, width=self.sw, height=self.sh, highlightthickness=0)
        self.canvas.place(x=0, y=0)
        self.pos1 = None
        self.pos2 = None
        self.window = None
        self.cancelled = False
        self.withdraw()
        def deferred():
            self.screen = grab(sct, mon)
            self.screen = ImageTk.PhotoImage(self.screen, master=self)
            self.canvas.create_image((0, 0), anchor=tk.NW, image=self.screen)
            self.overlay = self.make_rect(self.sw, self.sh, 'gray12')
            self.top_rect = self.make_rect()
            self.left_rect = self.make_rect()
            self.right_rect = self.make_rect()
            self.bottom_rect = self.make_rect()
            self.outline = self.canvas.create_rectangle(0, 0, 0, 0, outline='#7777ff', fill='')
            self.deiconify()
            self.lift()
            if drag_area:
                self.bind('<Motion>', self.drag_motion)
                self.bind('<ButtonPress-1>', self.drag_clicked)
                self.bind('<ButtonRelease-1>', self.released)
                self.bind('<ButtonPress-3>', self.set_cancelled)
            else:
                self.canvas.delete(self.overlay)
                if sys.platform == 'win32':
                    self.bind('<Motion>', self.scan_motion_win)
                else:
                    self.bind('<Motion>', self.scan_motion)
                self.bind('<ButtonPress-1>', self.scan_clicked)
                self.bind('<ButtonPress-3>', self.set_cancelled)
                e = tk.Event()
                e.x = self.winfo_pointerx()
                e.y = self.winfo_pointery()
                self.scan_motion(e)
        self.after(50, deferred)

    def make_rect(self, w=0, h=0, stipple='gray50'):
        return self.canvas.create_rectangle(0, 0, w, h, outline='', fill='black', stipple=stipple)

    def set_rect(self, lx, ly, rx, ry):
        self.canvas.coords(self.outline, lx, ly, rx, ry)
        self.canvas.coords(self.top_rect, 0, 0, self.sw, ly)
        self.canvas.coords(self.left_rect, 0, ly, lx, ry)
        self.canvas.coords(self.right_rect, rx, ly, self.sw, ry)
        self.canvas.coords(self.bottom_rect, 0, ry, self.sw, self.sh)

    def drag_motion(self, event):
        if self.pos1 is not None:
            self.pos2 = (event.x, event.y)
            self.set_rect(*get_spanned_rect(self.pos1, self.pos2))

    def drag_clicked(self, event):
        self.canvas.delete(self.overlay)
        self.pos1 = (event.x, event.y)
        self.drag_motion(event)

    def released(self, _event):
        self.withdraw()
        self.destroy()
        if self.root is not None:
            self.root.stop()
            self.root.destroy()
        self.event.set()

    def set_cancelled(self, event):
        logging.info(_('Window grab cancelled'))
        self.cancelled = True
        self.released(event)

    def scan_motion(self, event):
        window = pywinctl.getTopWindowAt(event.x, event.y)
        if window is not None:
            self.set_rect(*window.rect)

    def scan_motion_win(self, event):
        window = pywinctl.getActiveWindow()
        handle = window.getPID()
        windows = pywinctl.getWindowsAt(event.x, event.y)
        for sub in windows:
            if sub.getPID() != handle:
                window = sub
                break
        self.set_rect(*window.rect)

    def scan_clicked(self, event):
        self.pos1 = event.x, event.y
        self.released(event)

    async def get_window_and_bbox(self):
        await self.event.wait()
        if self.cancelled:
            return None, None
        window = pywinctl.getTopWindowAt(*self.pos1)
        bbox = Rect(*self.pos1, *self.pos2) if self.drag_area else None
        return window, bbox

async def get_window_and_bbox(drag_grab):
    return await WindowGrabber(drag_grab).get_window_and_bbox()

def get_root_window(window):
    pid = window.getPID()
    windows = { w.getHandle(): w for w in pywinctl.getAllWindows() if w.getPID() == pid }
    while True:
        phandle = window.getParent()
        if phandle in windows:
            window = windows[phandle]
        else:
            return window

async def get_window_from_pid(pid):
    result = None
    for window in pywinctl.getAllWindows():
        if window.getPID() == pid:
            result = window
    if result is None:
        logging.info(_('Could not find window automatically'))
        result, _bbox = await get_window_and_bbox(False)
    if result is not None:
        result = get_root_window(result)
    return result

class VideoWriterError(Exception):
    """cv2 没能创建视频文件 —— 最常见的原因是路径含中文而编码器不支持。"""

def is_ascii_path(path:Path) -> bool:
    try:
        return str(path).isascii()
    except Exception:
        return True

def video_writer_error_message(path:Path, codec:str) -> str:
    text = _('Cannot create video file: {}').format(path)
    if is_ascii_path(path):
        return text + '\n\n' + _('Codec {} is not available in this build.').format(codec)
    return text + '\n\n' + _('The path contains non-ASCII characters, which codec {} cannot handle. Switch the video type to webm/vp80, or move the folder to an ASCII-only path.').format(codec)

def _can_write_dir(folder:Path) -> bool:
    """真的往这个目录写一个字节再删掉 —— 只 ``mkdir`` 成功不代表写得进去。"""
    probe = folder / f'.at_wtest_{os.getpid()}_{time.time_ns()}'
    try:
        probe.write_bytes(b'0')
        probe.unlink()
        return True
    except OSError:
        return False

def temp_video_path(container:str, near:Path=None) -> Path:
    """给「写不进真实路径」的情况准备一个纯 ASCII 的临时落点。

    优先用**目标所在盘的根目录**：``D:\\sai2录屏\\缩时回放`` 里的中文只在子目录，
    ``D:\\`` 本身是 ASCII。这样 ``shutil.move`` 是同盘改名（瞬间完成），也不会
    把用户几百 MB 的录像先堆到 C 盘。

    根目录不可写时退回 ``%TEMP%`` —— 它一般是
    ``C:\\Users\\<名>\\AppData\\Local\\Temp``，用户名也可能是中文，那就再退到
    系统盘根目录。
    """
    stamp = f'at_video_{os.getpid()}_{time.time_ns()}'
    system_drive = (os.environ.get('SystemDrive', 'C:') + os.sep)
    bases = []
    if near is not None:
        try:
            # 系统盘例外：C 盘根目录在管理员账号下也可写，但那太显眼，还是走 %TEMP%
            if near.drive and near.drive.lower() != system_drive[:2].lower():
                bases.append(Path(near.drive + os.sep))
        except (OSError, ValueError):
            pass
    bases.append(Path(tempfile.gettempdir()))
    bases.append(Path(system_drive))
    seen = set()
    for base in bases:
        key = str(base).lower()
        if key in seen:
            continue
        seen.add(key)
        if not is_ascii_path(base) or not _can_write_dir(base):
            continue
        _sweep_stale_temp_videos(base)
        return base / f'{stamp}.{container}'
    # 实在没有 ASCII 地方可写，返回原样让上层报错，别静默丢帧
    return Path(tempfile.gettempdir()) / f'{stamp}.{container}'

def _sweep_stale_temp_videos(base:Path, max_age:float=86400.0):
    """清掉上次崩溃留下的中转文件 —— 一个长录像可能是几百 MB。

    只动一天以前的，且是正在被占用的文件删不掉（Windows 会拒绝），
    所以不会误删另开着的实例正在写的那个。
    """
    cutoff = time.time() - max_age
    try:
        leftovers = list(base.glob('at_video_*'))
    except OSError:
        return
    for path in leftovers:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass

def _probe_write(target:Path, container:str, codec:str, size:Size) -> bool:
    """往 ``target`` 真写两帧，返回是否落盘成功。写完不管成败都清掉。"""
    stamp = int(time.time() * 1000)
    probe = target.parent / f'.at_probe_{stamp}_{target.stem}.{container}'
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(str(probe), cv2.VideoWriter.fourcc(*codec), 10.0, size)
        try:
            frame = np.zeros((size.height, size.width, 3), dtype=np.uint8)
            writer.write(frame)
            writer.write(frame)
        finally:
            writer.release()
        ok = probe.exists() and probe.stat().st_size > 0
        if ok:
            # 文件落盘了还不够。avc1 缺 OpenH264 库时，VideoWriter 会写出一个
            # 只有文件头（约 1KB）的空壳，isOpened() 依然是 True —— 只看大小
            # 会判成功，用户录两小时拿到的是一堆打不开的文件。
            # 回读一次：帧数 > 0，或者至少能真解出一帧。
            cap = cv2.VideoCapture(str(probe))
            try:
                frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                ok = frames > 0
                if not ok:
                    # 有些容器不往元数据里写帧数，那就实读一帧
                    ok = bool(cap.read()[0])
            finally:
                cap.release()
            if not ok:
                logging.debug('Probe file for %s/%s is unreadable (%d bytes)', container,
                              codec, probe.stat().st_size)
    except Exception:
        logging.debug('Probe write failed', exc_info=True)
        ok = False
    # 按前缀扫一遍而不是只 unlink 精确名字：cv2 遇到非 ASCII 文件名时会把名字
    # 写成乱码（UTF-8 字节被当成 GBK），精确路径根本找不到那个文件，于是每次
    # 导出失败都会在用户的目录里留下一个 `.at_probe_xxx_姝ｆ瘮30.mp4`。
    for leftover in target.parent.glob(f'.at_probe_{stamp}_*'):
        try:
            leftover.unlink()
        except OSError:
            pass
    return ok

def probe_video_target(target:Path, container:str, codec:str, size=Size(64, 48)) -> bool:
    """在真实目标旁边试写两帧，确认「路径 + 容器 + 编码器」这个组合真的能落盘。

    cv2 在 Windows 上遇到非 ASCII 路径有两种静默失败，只看 ``isOpened()`` 不够：
    - 目录名含中文 → ``isOpened()`` 为 False，文件不生成；
    - 文件名含中文 → ``isOpened()`` 为 True，但文件同样不生成。
    所以必须真写一次、检查文件是否出现。

    ``target`` 传最终要写的文件（导出）或会话目录（录制）——探针文件名会带上
    它的名字，这样目录名和文件名的字符集都被覆盖到。

    **返回 True 表示要绕道临时文件**：直接写失败了，但换成纯 ASCII 的临时路径
    就能写。这不是失败，是「这个编码器吃不了非 ASCII 名字」，换个地方写、
    写完再搬回去就行 —— 文件内容跟名字无关。目前只有 avc1 会这样，
    vp80 / mp4v 走 FFmpeg，中文路径本来就没问题。
    """
    target = expand_path(target)
    created_parent = not target.parent.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    if _probe_write(target, container, codec, size):
        return False
    # 直接写不进去。如果路径确实含非 ASCII，多半是编码器不接受这种名字 ——
    # 换成纯 ASCII 的临时路径再试一次，能写就说明「换个地方写、写完搬回来」可行。
    if not is_ascii_path(target):
        if _probe_write(temp_video_path(container, near=target), container, codec, size):
            logging.info(
                _('Codec {} cannot write to a non-ASCII path; encoding through a temporary file instead.').format(codec)
            )
            return True
    if created_parent:
        # 目录是这次探测建的，又什么都没写成 —— 别留一个空的会话目录在那儿，
        # 导出页会把它列成「0 帧」的会话。
        try:
            target.parent.rmdir()
        except OSError:
            pass
    raise VideoWriterError(video_writer_error_message(target, codec))


def negotiate_video_format(target: Path, container: str, codec: str) -> tuple[str, str, bool]:
    """试写一次，avc1 不可用时退到能用的编码器。返回 ``(container, codec, via_temp)``。

    avc1（H.264）在 OpenCV 里依赖外部的 OpenH264 库。2026-11-19 那台 ASUS 笔记本上
    它直接 ``isOpened() == False``，程序留下一句「当前版本不支持编码器 avc1」就把录制
    停了，用户一帧都没录到 —— 而他并没有选错任何东西，是机器上缺库。

    打包已经把库带上了，但环境千奇百怪（缺 VC 运行库、被安全软件拦、精简版系统），
    与其让用户卡在这一步，不如自己退一步：mp4v 还是 mp4 容器、还是 .mp4 扩展名，
    画质略逊但能录。webm/vp80 兜底。

    用户显式选了别的编码器时不动它 —— 失败就失败，那是他自己填的值，硬换更糟。
    """
    attempts = [(container, codec)]
    if codec.lower() in ('avc1', 'h264'):
        attempts.append(('mp4', 'mp4v'))
        attempts.append(('webm', 'vp80'))

    last_error = None
    failed = []
    for index, (cont, cod) in enumerate(attempts):
        try:
            via_temp = probe_video_target(target, cont, cod)
        except Exception as exc:
            last_error = exc
            failed.append(cod)
            logging.debug('Codec %s is unusable here: %s', cod, exc)
            continue
        if index > 0:
            logging.warning(
                _('Codec {} is not available on this machine; falling back to {} ({}).')
                .format(codec, cod, cont))
        return cont, cod, via_temp

    if last_error is not None:
        # 把试过的编码器全列出来。只说「avc1 不支持」的话，用户会以为换个编码器
        # 就行 —— 实际上 mp4v 和 vp80 也一起失败了（2026-11-19 有人用 v1.10 报
        # 这个错，日志里只有 avc1 一行，看不出降级也失败了）。
        raise VideoWriterError(
            video_writer_error_message(target, codec) + '\n\n'
            + _('Tried: {}').format(' / '.join(failed) or codec))
    raise VideoWriterError(video_writer_error_message(target, codec))


# Handles output to a video file.
class VideoWriter:
    def __init__(self, file_path:Path, size:Size, container:str, codec:str, fps=FPS, log=False, via_temp=False):
        """``via_temp=True`` 时先写到纯 ASCII 的临时文件，``close()`` 时再搬回来。

        给 avc1 用的：它在 Windows 上只认 ASCII 路径，中文目录或中文文件名都
        会让它一帧都写不进去。文件内容跟文件名无关，换个地方写完搬回去，
        成片还是落在用户要的位置、还叫画布名。
        """
        self.size = even_size(size)
        # 兜底：尺寸非法时不能说「编码器不支持」。宽高为 0 是上游读错了内存
        # （版本参数不匹配），跟编码器毫无关系 —— 归错因会把用户引到错误的路上。
        if self.size.width <= 0 or self.size.height <= 0:
            raise VideoWriterError(
                _('Canvas size is {}x{} — the SAI version parameters do not match, '
                  'so nothing can be recorded. Run Sai2-Sniffer and send the report back.')
                .format(size.width, size.height))
        self.file_path = expand_path(file_path).with_suffix(f'.{container}')
        self.via_temp = via_temp
        self.temp_path = temp_video_path(container, near=self.file_path) if via_temp else None
        target = self.temp_path or self.file_path
        if log:
            logging.info(_('Writing to video:') + f' {self.file_path} ({codec})')
            if self.temp_path is not None:
                logging.info(_('Codec {} cannot write to a non-ASCII path; encoding through a temporary file instead.').format(codec))
        self.writer = cv2.VideoWriter(str(target), cv2.VideoWriter.fourcc(*codec), fps, self.size)
        if not self.writer.isOpened():
            # 探测用 64x48 试写，真实尺寸可能是 7000x7000 —— 探测通过、真写失败
            # 是完全可能的。错误信息里必须带上尺寸和实际落点，否则只剩一句
            # 「无法创建视频文件」，看不出是编码器缺库还是幅面/权限的问题。
            # Windows 上 cv2 遇到非 ASCII 路径会「静默失败」：isOpened() 为 False，
            # 文件根本不会生成，也不抛异常。不检查的话录制看起来一切正常，
            # 实际一帧都没写进去，用户只能从「导出时没有帧」反推。
            raise VideoWriterError(
                video_writer_error_message(self.file_path, codec) + '\n\n'
                + _('Size {}x{}, {} fps, writing to {}').format(
                    self.size.width, self.size.height, fps, target))

    def __enter__(self):
        return self

    def __exit__(self, *exc_args):
        self.close()

    def close(self):
        self.writer.release()
        if self.temp_path is None:
            return
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.temp_path), str(self.file_path))
        except Exception:
            # 搬不动就留着临时文件 —— 至少成片还在，用户能自己找回来，
            # 比默默丢掉强。export() 之后会检查目标是否存在并报错。
            logging.error(_('Could not move the temporary video into place:') + f' {self.temp_path} -> {self.file_path}', exc_info=True)
        self.temp_path = None

    def write(self, img:np.ndarray, cvt_color=False, reuse_arrays:dict=None):
        img = image_fit_and_pad(img, self.size, reuse_arrays=reuse_arrays)
        if cvt_color:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        self.writer.write(img)

# Handles input from a video file.
class VideoReader:
    def __init__(self, file_path:Path):
        self.video_reader = cv2.VideoCapture(str(expand_path(file_path)))
        self.data = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_args):
        self.close()

    def close(self):
        self.video_reader.release()

    def read(self):
        ret, self.data = self.video_reader.read(self.data)
        return self.data if ret else None

    def get_frame_count(self):
        return int(self.video_reader.get(cv2.CAP_PROP_FRAME_COUNT))

    def get_size(self) -> Size:
        return Size(int(self.video_reader.get(cv2.CAP_PROP_FRAME_WIDTH)), int(self.video_reader.get(cv2.CAP_PROP_FRAME_HEIGHT)))

# Automatically open and close video writers if the input images change size.
class VideoSequenceWriter:
    def __init__(self, frames_path:Path, container:str, codec:str, frames_auto_split_count=500, via_temp=False):
        self.container = container
        self.codec = codec
        self.folder = expand_path(frames_path)
        self.frames_auto_split_count = frames_auto_split_count
        self.frames_count = 0
        # 跨分片的累计帧数，给界面显示「已录多少」用（frames_count 每次分段会归零）
        self.total_frames = 0
        # 会话目录在中文路径下、编码器又是 avc1 时要绕道临时文件；由调用方探测后传进来
        self.via_temp = via_temp
        logging.info(_('Writing to frames folder:') + f' {self.folder}; container: {container}; codec: {codec}')
        self.folder.mkdir(parents=True, exist_ok=True)
        self.writer = None
        self.reuse_arrays = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc_args):
        self.close()

    def close(self):
        if self.writer is not None:
            self.writer.close()

    def open_new(self, size:Size):
        self.writer = VideoWriter(self.folder / f'{time.time_ns()}', size, self.container, self.codec, via_temp=self.via_temp)
        self.frames_count = 0

    def check_new(self, size:Size):
        if self.writer is None:
            self.open_new(size)
        elif even_size(size) != self.writer.size or (self.frames_auto_split_count != 0 and self.frames_count >= self.frames_auto_split_count):
            self.writer.close()
            self.open_new(size)

    def write(self, img:np.ndarray, cvt_color=False):
        self.check_new(get_size(img))
        self.writer.write(img, cvt_color, reuse_arrays=self.reuse_arrays)
        self.frames_count += 1
        self.total_frames += 1

# Read a folder of mp4 files as if it were a single contiguous file.
def scan_frames(folder:Path):
    """统计帧目录下所有分片的总帧数与最大画幅（只读元数据，不解码画面）。

    返回 ``(files, frame_count, size)``。导出前的成片预估也走这里，
    避免和 ``VideoSequenceReader`` 各写一份 glob 逻辑、口径不一致。
    """
    folder = expand_path(folder)
    files = []
    frame_count = 0
    size = Size(0, 0)
    if not folder.is_dir():
        return files, frame_count, size
    for file in sorted(folder.glob('*')):
        # 跳过 .at_probe_* 之类的临时探针文件，别让它们混进帧计数
        if file.name.startswith('.'):
            continue
        # 只认真实文件。帧目录留空时会退回当前工作目录，那里全是 build/dist/venv
        # 这种子目录，cv2 会挨个当视频打开并在 stderr 刷
        # "imread_('build'): can't open/read file"，白扫一遍还吵。
        if not file.is_file():
            continue
        reader = VideoReader(file)
        try:
            sub_frame_count = reader.get_frame_count()
            if sub_frame_count > 0:
                files.append(file)
                frame_count += sub_frame_count
                size = max_size(size, reader.get_size())
        finally:
            # 以前这里从不释放，扫几十个分片会攒下一堆没关的 VideoCapture。
            reader.close()
    return files, frame_count, size

class VideoSequenceReader:
    def __init__(self, folder:Path):
        self.folder = expand_path(folder)
        logging.info(_('Reading from frames folder:') + f' {self.folder}')
        self.reader = None
        self.files, self.frame_count, self.size = scan_frames(self.folder)
        self.file_index = 0
        self.load_next_video()

    def __enter__(self):
        return self

    def __exit__(self, *exc_args):
        self.close()

    def close(self):
        if self.reader is not None:
            self.reader.close()

    def get_last_frame(self):
        reader = VideoReader(self.files[-1])
        last_frame = None
        while True:
            data = reader.read()
            if data is not None:
                last_frame = data
            else:
                break
        reader.close()
        return last_frame

    def load_next_video(self):
        if self.file_index < len(self.files):
            if self.reader is not None:
                self.reader.close()
            self.reader = VideoReader(self.files[self.file_index])
            self.file_index += 1

    def read(self):
        data = self.reader.read()
        if data is None:
            self.load_next_video()
            data = self.reader.read()
        return data

class EventTrackerBase:
    def __init__(self):
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()

    def __enter__(self):
        return self

    def __exit__(self, *exc_args):
        self.stop()

    def stop(self):
        pass

    def emit_event(self):
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    def stop_event_stream(self):
        self._queue.shutdown()

    async def get_event(self):
        await self._queue.get()

    def drain(self):
        """丢掉队列里堆着的事件。

        暂停期间用户还在画，点击照旧入队；恢复时若原样处理，会连着读很多次
        画布内存（每次都是一趟跨进程调用），而它们最终都会被 ``is_image_equal``
        跳过。只留恢复那一刻抓的一帧就够了。
        """
        while True:
            try:
                self._queue.get_nowait()
            except Exception:
                return

    async def get_event_stream(self):
        try:
            while True:
                yield await self.get_event()
        except (asyncio.CancelledError, asyncio.QueueShutDown):
            return

# Track mouse clicks on a window.
class InputTracker(EventTrackerBase):
    def __init__(self, target_window, bbox=None):
        super().__init__()
        self._title = target_window.title
        self.target_window = target_window
        self.bbox = bbox
        self.click_started_in_window = False
        self.start()

    def on_click_callback(self, x, y, _button, is_pressed):
        try:
            if is_pressed:
                window = pywinctl.getTopWindowAt(x, y)
                if window is None:
                    return
                self.click_started_in_window = window.getHandle() == self.target_window.getHandle()
                if self.click_started_in_window:
                    bbox = window.rect if self.bbox is None else self.bbox
                    self.click_started_in_window = point_in_bbox(bbox, (x, y))
            if not is_pressed and self.click_started_in_window:
                self.click_started_in_window = False
                self.emit_event()
        except:
            pass

    async def track_window(self):
        while self.target_window.isAlive:
            try:
                self._title = self.target_window.title
            except:
                pass
            await asyncio.sleep(0.1)
        logging.info(_('Window lost:') + f' {self._title}')
        self.stop_event_stream()

    def start(self):
        logging.info(_('Tracking input for window:') + f' {self._title}')
        self.mouse_listener = pynput.mouse.Listener(on_click=self.on_click_callback)
        self.mouse_listener.start()
        self._wait_task = asyncio.create_task(self.track_window())

    def stop(self):
        self.mouse_listener.stop()
        # stop() 只是「请求」停止，真正的 UnhookWindowsHookEx 发生在监听线程里。
        # 不 join 就退进程，这个 WH_MOUSE_LL 低级钩子会留在系统里，
        # 之后每条鼠标事件都要等它超时 —— 用户看到的就是关掉程序后鼠标卡几秒。
        # 有界 join：正常几十毫秒就结束，卡住也不会把界面拖死。
        try:
            self.mouse_listener.join(0.5)
        except Exception:
            logging.debug('Mouse listener did not stop in time', exc_info=True)
        self._wait_task.cancel()
        logging.info(_('Stopped tracking window:') + f' {self._title}')

# Get frame indices achieve a specific maximum video length.
def get_frame_indices(export_time_limit:float, frame_count:int, fps:int):
    if export_time_limit > 0:
        target_frames = int(export_time_limit * fps)
        if frame_count > target_frames:
            nth = frame_count / target_frames
            return target_frames, (round(i * nth) for i in range(target_frames))
    return frame_count, range(frame_count)

def write_preview_frames(progress_iter:function, reader:VideoSequenceReader, writer:VideoSequenceWriter, preview_duration:float, fps:int, reuse_arrays:dict):
    last_frame = reader.get_last_frame()
    if preview_duration == 0:
        writer.write(last_frame, reuse_arrays=reuse_arrays)
    else:
        frame_count = int(preview_duration * fps)
        for _ in progress_iter(range(frame_count), frame_count, unit='frames'):
            writer.write(last_frame, reuse_arrays=reuse_arrays)

class NoFrameData(Exception):
    """帧目录里一帧都没有 —— 通常是还没开始录制就点了导出。"""

def unique_path(path:Path) -> Path:
    """同名文件已存在时自动加 ``_1``、``_2``……，不静默覆盖上一次的成片。"""
    path = expand_path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    i = 1
    while True:
        candidate = parent / f'{stem}_{i}{suffix}'
        if not candidate.exists():
            return candidate
        i += 1

def export(progress_iter:function, export_time_limit:float, preview_last_frame:bool, preview_duration:float, fps:int, frames:Path, container:str, codec:str, output_path='', output_name=''):
    frames = expand_path(frames)
    if not (frames.exists() and frames.is_dir()):
        raise Exception(_('No appropriate frame data found:') + f' {frames}')
    if output_path == '':
        # 留空时输出到帧目录旁边、用会话名命名。
        # 不能用 with_suffix：目录名里带点（如 "回放.v2"）会被当成扩展名替换掉，
        # 成片就跑到父级目录去了，还可能覆盖同名文件。
        output_path = frames.parent / f'{frames.name}.{container}'
    output_path = expand_path(output_path)
    if output_path.is_dir():
        # 用户给的是目录 → 用会话名自动命名
        output_path = output_path / f'{frames.name}.{container}'
    # 扩展名统一成容器的：`VideoWriter` 里会用 `with_suffix` 改一次，这里先对齐，
    # 否则传进来的路径没扩展名（`D:/out/clip`）时，返回值和真正写出的文件
    # （`clip.mp4`）对不上，界面「打开文件夹」就会找不到文件。
    if output_path.suffix.lower() != f'.{container}':
        output_path = output_path.with_suffix(f'.{container}')
    suffix = output_path.suffix
    # 优先用画布名（`正比30.mp4` 比 `20261101_143611.mp4` 好认），写不了再退回
    # 时间戳名。mp4/avc1 在含中文的路径或文件名上会静默失败，而画布名常常就是
    # 中文 —— 退回一步，至少保证导出本身能成功，别为了好看的名字把成片搞丢。
    names = []
    for name in (output_name, frames.name):
        if name and name not in names:
            names.append(name)
    last_error = None
    via_temp = False
    # 先确认这套「容器 + 编码器」在本机能写，不行就退到 mp4v / vp80。
    # 放在文件名循环之前：编码器不可用和文件名非法是两回事，
    # 混在同一个循环里，报错会指错方向（用户看到「名字不能用」但其实是缺编码器）。
    container, codec, _ = negotiate_video_format(
        unique_path(output_path.parent / f'{output_name or frames.name}{suffix}'),
        container, codec)
    # 降级可能连容器一起换了（mp4/avc1 → webm/vp80），扩展名得跟着改，
    # 否则会出现「.mp4 里装 webm」，播放器和剪辑软件都会认错。
    if output_path.suffix.lower() != f'.{container}':
        output_path = output_path.with_suffix(f'.{container}')
        suffix = output_path.suffix
    for index, name in enumerate(names):
        candidate = unique_path(output_path.parent / f'{name}{suffix}')
        try:
            via_temp = probe_video_target(candidate, container, codec)
        except VideoWriterError as ex:
            last_error = ex
            if index == len(names) - 1:
                raise
            logging.warning('Cannot name the output "%s" (%s), falling back to "%s"',
                            name, ex, names[index + 1])
            continue
        output_path = candidate
        break
    else:                                   # names 为空时才会走到（理论上不会）
        raise last_error or VideoWriterError(_('No appropriate frame data found:'))
    with VideoSequenceReader(frames) as reader:
        if reader.frame_count == 0:
            raise NoFrameData(_('No frames have been recorded yet. Start recording first, then export.'))
        total_frames, frame_indices = get_frame_indices(export_time_limit, reader.frame_count, fps)
        index = 0
        reuse_arrays = {}
        with VideoWriter(output_path, reader.size, container, codec, fps=fps, log=True, via_temp=via_temp) as writer:
            if preview_last_frame:
                write_preview_frames(progress_iter, reader, writer, preview_duration, fps, reuse_arrays)
            for frame in progress_iter(frame_indices, total_frames, unit='frames'):
                while index < frame:
                    reader.read()
                    index += 1
                data = reader.read()
                index += 1
                writer.write(data, reuse_arrays=reuse_arrays)
    if via_temp and not output_path.exists():
        # 走临时文件时最后一步是 shutil.move，搬失败会留在临时目录里。
        # 这时候不能报「导出成功」—— 用户去文件夹里找会找不到。
        raise VideoWriterError(video_writer_error_message(output_path, codec))
    # 返回真实落点：界面要显示「导出到哪了」，也是「打开文件夹」按钮的依据
    return output_path

def copy_into(dst:np.ndarray, src:np.ndarray) -> np.ndarray:
    if dst is not None and dst.shape == src.shape:
        np.copyto(dst, src)
        return dst
    else:
        return src.copy()

async def sai_capture(sai_proc:sai.SAI, canvas, image_size_limit:int, frames:Path, container:str, codec:str, auto_split_count:int, on_progress=None, paused=None):
    """录制 SAI 画布，直到用户停止。

    ``paused`` 是 ``asyncio.Event``：置位表示暂停中。暂停期间不抓帧，但
    writer 和会话目录都保持打开 —— 恢复后接着往同一个视频里写，不会另起一段。

    返回结束原因：``'canvas_lost'``（画布被关掉）、``'no_window'``（找不到窗口）、
    ``'stopped'``（正常停止）。
    """
    window = await get_window_from_pid(sai_proc.get_pid())
    image_size_limit_size = Size(image_size_limit, image_size_limit)
    thumbnail = None
    if window is None:
        return 'no_window'
    # 先确认这个目录/格式真的写得进去，别让用户画了一小时才发现一帧没存。
    # 返回 True 表示编码器吃不了这个非 ASCII 路径，要绕道临时文件 —— 不是失败。
    # 三个返回值都要 —— 降级换了编码器的话，下面建 writer 得用换过的
    container, codec, via_temp = negotiate_video_format(
        frames.parent / frames.name, container, codec)
    with VideoSequenceWriter(frames, container, codec, auto_split_count, via_temp=via_temp) as writer, InputTracker(window) as tracker:
        last_img = None
        reason = 'stopped'
        async for _event in tracker.get_event_stream():
            if paused is not None and paused.is_set():
                # 挂在这里等恢复。期间画的东西不会被逐笔记录 —— 恢复时抓的
                # 这一帧就是暂停结束时的画面，对缩时回放来说正是想要的效果。
                await paused.wait()
                tracker.drain()
            if not sai_proc.api.check_if_canvas_exists(canvas):
                logging.info(_('Canvas lost, stopping now'))
                reason = 'canvas_lost'
                break
            # 等 SAI 把重绘做完再读，否则会读到 tile 更新的中间态（横向红条）。
            await asyncio.sleep(REDRAW_SETTLE_SEC)
            tracker.drain()     # 这段等待里积压的事件不用逐个抓：画布已是最终状态
            map_level = sai_proc.api.get_map_level_for_size(canvas, image_size_limit)
            img = sai_proc.api.get_canvas_image(canvas, map_level)
            if is_image_equal(last_img, img):
                continue
            last_img = copy_into(last_img, img)
            thumbnail = image_thumbnail(img, image_size_limit_size, thumbnail)
            writer.write(thumbnail)
            if on_progress is not None:
                on_progress(writer.total_frames)
    if writer.total_frames == 0:
        # 一帧都没录到（画布刚开就被关、按错了立刻停）：把空目录收掉。
        # 留着的话导出页会把它当成一次「0 帧」的会话列出来，越攒越多。
        #
        # 注意：`rmdir` 只在目录**完全为空**时成功，而录制开始时写过 session.json，
        # 所以光调 rmdir 会失败、留下一个只有元数据的空壳（2026-11-19 实测发现）。
        # 只在「目录里除了 session.json 没有别的东西」时才连元数据一起删 ——
        # 有任何其它文件（哪怕一帧）都不动。
        try:
            entries = list(writer.folder.iterdir())
            if len(entries) == 1 and entries[0].name == SESSION_META:
                entries[0].unlink()
            writer.folder.rmdir()
        except OSError:
            pass
    return reason
