"""录制悬浮条 —— 置顶小条，开始/暂停/停止录制，随时看录了多久、多少帧。

为什么用 Tk 而不是第二个 WebView2 窗口：

* 第二个 WebView2 实例要再吃 ~50MB 内存，而这里只有几个文字和三个按钮；
* Tk 能用 ``SetWindowRgn`` 裁出真圆角窗口（区域外完全不属于窗口），
  WebView2 的 frameless 窗口是矩形，想做出「浮在桌面上的小卡片」得走分层窗口；
* 拖动在 Tk 里就是 ``B1-Motion`` 改 geometry，不用跨进程 IPC。

线程模型：Tk 必须在自己的线程里跑 mainloop，而主线程被 ``webview.start()`` 占着，
所以这里起一个 daemon 线程跑 Tk。跨线程访问 backend 是安全的 ——
``Backend.submit()`` 本来就是给任意线程用的（``run_coroutine_threadsafe``），
全局热键线程已经这么干；``status()`` 只读普通属性。

尺寸：``BASE_W x BASE_H`` 是**上限**，实际按 ``float_scale``（0.45~1.0）等比缩。
拖右下角那个小把手可以改，松手记进设置。布局是流式的（文字从左排、
按钮从右往左），所以缩到某一档放不下「帧数」时会自动让位 ——
固定坐标等比缩到 0.6 会让数字和按钮叠在一起。

这里**不写** backend 的任何内部状态，只调它已有的公开方法。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import tkinter as tk
from tkinter import font as tkfont

from .util import format_duration

# ---- 配色（与 webui/style.css 的 sai-dark 同一族）----
BG = '#14171a'
EDGE = '#232930'      # 1px 描边，只求在深色桌面上分得出边界，别抢眼
FG = '#e4e7ea'
MUTED = '#8d949c'
DIM = '#4e555c'
RED = '#c2554c'
AMBER = '#d0a24a'
BLUE = '#5b87ab'
HOVER = '#232a30'

# 上限尺寸。所有坐标、字号都按这两个数等比缩。
BASE_W, BASE_H = 302, 58
BASE_RADIUS = 15

MIN_SCALE = 0.45      # 约 136x26，实测这一档「录制中 / 0:03 / 142 帧」仍然读得清
MAX_SCALE = 1.0
# 0.5 = 151x29，面积正好是原来的四分之一。底部把手能拖到 1.0（302x58）。
DEFAULT_SCALE = 0.5


def _clamp(value, low, high):
    return max(low, min(high, value))


class FloatBar:
    """一个 Tk 悬浮条。调用 ``start()`` 起线程，``stop()`` 让它自己收尾。"""

    def __init__(self, backend):
        self.backend = backend
        self.root = None
        self._thread = None
        self._closing = False
        self._dpi = 1.0
        self.scale = DEFAULT_SCALE
        self._drag = None
        self._drag_moved = False
        self._resize = None
        self._save_job = None
        # 控件引用（_create_widgets 里填）
        self._edge = None
        self._body = None
        self._dot = None
        self._dot_id = None
        self._status_lbl = None
        self._time_lbl = None
        self._frame_lbl = None
        self._main_btn = None
        self._stop_btn = None
        self._grip = None

    # ------------------------------------------------------------ 生命周期
    def start(self):
        self._thread = threading.Thread(target=self._run, name='floatbar', daemon=True)
        self._thread.start()

    def stop(self):
        """从别的线程请求退出。Tk 对象不能跨线程碰，交给 _tick 自己收。"""
        self._closing = True

    def _run(self):
        try:
            root = tk.Tk()
        except Exception:
            logging.exception('Could not create the floating bar')
            return
        self.root = root
        try:
            self._create_widgets(root)
            root.mainloop()
        except Exception:
            logging.exception('Floating bar crashed')
        finally:
            try:
                root.destroy()
            except Exception:
                pass
            self.root = None
            # widget 对象持有 master 链，间接拽着 Tk 解释器。不逐个清掉的话，
            # 解释器会等到主线程 GC 时才析构，于是报 "Tcl interpreter is leaked"。
            for name in ('_edge', '_body', '_dot', '_status_lbl', '_time_lbl',
                         '_frame_lbl', '_main_btn', '_stop_btn', '_grip'):
                setattr(self, name, None)
            try:
                if getattr(tk, '_default_root', None) is root:
                    tk._default_root = None
            except Exception:
                pass

    # ------------------------------------------------------------ 建控件
    def _create_widgets(self, root: tk.Tk):
        # 主程序进程已经是 per-monitor DPI aware（WebView2 设的），
        # 这里拿到的是物理像素，得自己按 DPI 放大，否则 150% 缩放下是条小板砖。
        self._dpi = max(1.0, root.winfo_fpixels('1i') / 96.0)
        try:
            saved_scale = float(self.backend.store.get('float_scale') or DEFAULT_SCALE)
        except (TypeError, ValueError):
            saved_scale = DEFAULT_SCALE
        self.scale = _clamp(saved_scale, MIN_SCALE, MAX_SCALE)

        root.overrideredirect(True)
        root.attributes('-topmost', True)
        root.configure(bg=BG)

        # 外框：1px 描边靠一个略大的 Frame 垫底实现（Tk 没有 border 圆角）
        self._edge = tk.Frame(root, bg=EDGE)
        self._body = tk.Frame(self._edge, bg=BG)

        self._dot = tk.Canvas(self._body, highlightthickness=0, bg=BG)
        self._dot_id = self._dot.create_oval(0, 0, 1, 1, fill=DIM, outline='')

        self._status_lbl = tk.Label(self._body, text='未录制', bg=BG, fg=FG)
        self._time_lbl = tk.Label(self._body, text='', bg=BG, fg=FG)
        self._frame_lbl = tk.Label(self._body, text='', bg=BG, fg=MUTED)
        self._main_btn = self._button(self._body, 'play', self._on_main)
        self._stop_btn = self._button(self._body, 'stop', self._on_stop)

        # 缩放把手：右下角一格，光标换成斜向箭头就是提示
        self._grip = tk.Canvas(self._body, highlightthickness=0, bg=BG,
                               cursor='size_nw_se')
        self._grip.bind('<Button-1>', self._resize_start)
        self._grip.bind('<B1-Motion>', self._resize_move)
        self._grip.bind('<ButtonRelease-1>', self._resize_end)

        # 拖动：所有静态控件都绑，按钮和把手不绑（不然点按钮会变成拖窗口）
        for widget in (self._body, self._edge, self._dot,
                       self._status_lbl, self._time_lbl, self._frame_lbl):
            self._bind_drag(widget)

        root.bind('<Escape>', lambda e: self.stop())

        self._apply_scale(self.scale, initial=True)
        root.after(80, self._tick)

    # ------------------------------------------------------------ 缩放与布局
    def _px(self, value: float) -> int:
        return max(1, int(round(value * self.scale * self._dpi)))

    def _font_px(self, value: float, floor: int) -> int:
        """字号跟着缩放走，但有下限 —— 7px 的字等于没了。负数 = 像素高度。"""
        return -max(floor, int(round(value * self.scale * self._dpi)))

    def _apply_scale(self, scale: float, initial: bool = False):
        """重设窗口尺寸并把所有元素重新摆一遍。

        只改 geometry 不重排是不行的：坐标按比例缩、字号又有下限，
        两边不同步时就会叠在一起（第一版 0.6 档的数字直接压到按钮上）。
        """
        self.scale = _clamp(scale, MIN_SCALE, MAX_SCALE)
        root = self.root
        w = self._px(BASE_W)
        h = self._px(BASE_H)

        if initial:
            x, y = self._initial_position(w, h)
        else:
            x, y = root.winfo_x(), root.winfo_y()
        root.geometry(f'{w}x{h}+{x}+{y}')

        pad = self._px(1)
        self._edge.place(x=0, y=0, width=w, height=h)
        self._body.place(x=pad, y=pad, width=w - pad * 2, height=h - pad * 2)

        self._layout(w, h)
        self._round_region(self._px(BASE_RADIUS))

    def _layout(self, w: int, h: int):
        """流式排布：文字从左边往右排，按钮从右边往左排，中间让给文字。"""
        inner_h = h - self._px(2)

        # --- 右侧按钮（先算，因为左边能用多少取决于它们）
        btn = self._px(30)
        btn_gap = self._px(8)
        right_pad = self._px(10)
        stop_x = w - right_pad - btn
        main_x = stop_x - btn_gap - btn
        btn_y = (inner_h - btn) // 2
        self._main_btn.place(x=main_x, y=btn_y, width=btn, height=btn)
        self._stop_btn.place(x=stop_x, y=btn_y, width=btn, height=btn)
        for canvas in (self._main_btn, self._stop_btn):
            canvas._icon_w, canvas._icon_h = btn, btn
            canvas._redraw()

        # --- 缩放把手：右下角，压在按钮下方的空白里
        g = max(8, self._px(12))
        self._grip.place(x=w - g - self._px(2), y=h - g - self._px(2),
                         width=g, height=g)
        self._grip.delete('all')
        dot_r = max(1, g // 10)
        for i in (1, 2):
            off = self._px(2) + (g // 4) * i
            cx, cy = g - off, g - off
            self._grip.create_oval(cx - dot_r, cy - dot_r,
                                   cx + dot_r, cy + dot_r, fill=DIM, outline='')

        # --- 左侧：状态点 + 文字
        left = self._px(12)
        gap = self._px(7)
        dot = max(5, self._px(9))
        self._dot.place(x=left, y=(inner_h - dot) // 2, width=dot, height=dot)
        self._dot.coords(self._dot_id, 0, 0, dot, dot)
        x = left + dot + gap

        fs_status = self._font_px(13, 9)
        fs_time = self._font_px(15, 10)
        fs_small = self._font_px(11, 8)
        self._status_lbl.configure(font=('Microsoft YaHei UI', fs_status))
        self._time_lbl.configure(font=('Consolas', fs_time, 'bold'))
        self._frame_lbl.configure(font=('Microsoft YaHei UI', fs_small))

        # 文字宽度用 measure 算，别问 widget —— 这时它还没排过版，reqwidth 是旧值
        f_status = tkfont.Font(family='Microsoft YaHei UI', size=fs_status)
        f_time = tkfont.Font(family='Consolas', size=fs_time, weight='bold')
        f_small = tkfont.Font(family='Microsoft YaHei UI', size=fs_small)

        status_w = f_status.measure(self._status_lbl.cget('text') or '未录制')
        time_w = f_time.measure(self._time_lbl.cget('text') or '0:00')
        frame_w = f_small.measure(self._frame_lbl.cget('text') or '999 帧')

        avail = main_x - self._px(6) - x
        show_time = self._time_lbl.cget('text') != ''
        show_frame = self._frame_lbl.cget('text') != ''

        # 放不下就按「帧数 → 时长」的顺序让位。
        if show_frame and status_w + gap + time_w + gap + frame_w > avail:
            show_frame = False
        if show_time and status_w + gap + time_w > avail:
            show_time = False

        self._status_lbl.place(x=x, y=(inner_h - self._px(18)) // 2)
        x += status_w + gap

        if show_time:
            self._time_lbl.place(x=x, y=(inner_h - self._px(19)) // 2)
            x += time_w + gap
        else:
            self._time_lbl.place_forget()

        if show_frame:
            self._frame_lbl.place(x=x, y=(inner_h - self._px(16)) // 2 + self._px(1))
        else:
            self._frame_lbl.place_forget()

    def _initial_position(self, w: int, h: int) -> tuple[int, int]:
        """记住上次拖到哪；没有记录就贴右下角（任务栏上方）。"""
        saved = str(self.backend.store.get('float_pos') or '')
        if ',' in saved:
            try:
                x, y = (int(v) for v in saved.split(',', 1))
                sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
                # 换了显示器或者分辨率变小，旧坐标可能跑到屏幕外
                if -w + 80 < x < sw - 80 and -10 < y < sh - 40:
                    return x, y
            except ValueError:
                pass
        return (self.root.winfo_screenwidth() - w - self._px(48),
                self.root.winfo_screenheight() - h - self._px(96))

    def _round_region(self, radius: int):
        """把窗口裁成圆角。

        Tk 的 ``winfo_id()`` 给的是内部子窗口，顶层要再 GetParent 一次 ——
        直接对子窗口 SetWindowRgn 只会裁内部区域，窗口本身还是方块。
        """
        try:
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            self.root.update_idletasks()
            hwnd = self.root.winfo_id()
            top = user32.GetParent(hwnd) or hwnd
            w, h = self.root.winfo_width(), self.root.winfo_height()
            rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
            if not user32.SetWindowRgn(top, rgn, True):
                gdi32.DeleteObject(rgn)
        except Exception:
            logging.debug('Could not round the floating bar', exc_info=True)

    # ------------------------------------------------------------ 控件
    def _button(self, parent, kind: str, command) -> tk.Canvas:
        c = tk.Canvas(parent, highlightthickness=0, bg=BG, cursor='hand2')
        c._kind = kind
        c._hover = False
        # 注意别用 _w / _h 存尺寸：tkinter 的 Widget 用 self._w 记 Tk 控件路径名，
        # 覆盖成数字之后所有 Tk 调用都会变成 invalid command name "30"。
        c._icon_w = c._icon_h = 30

        def redraw():
            c.delete('all')
            w, h = c._icon_w, c._icon_h
            # 悬停底色先铺，不能画在图标之后 —— 后画的会盖住图标
            if c._hover and getattr(c, '_enabled', True):
                c.create_rectangle(0, self._px(2), w - 1, h - self._px(2),
                                   fill=HOVER, outline='')
            # 禁用时图标压暗，但形状还画得出来 —— 完全隐藏会让人以为按钮没了
            if not getattr(c, '_enabled', True):
                color = DIM
            else:
                color = FG if c._hover else MUTED
            if c._kind == 'play':
                c.create_polygon(w * 0.32, h * 0.26, w * 0.32, h * 0.74,
                                 w * 0.78, h * 0.5, fill=color, outline='')
            elif c._kind == 'pause':
                bw = max(2, int(w * 0.11))
                gap = max(2, int(w * 0.11))
                cx = w / 2
                c.create_rectangle(cx - gap - bw, h * 0.30, cx - gap, h * 0.70,
                                   fill=color, outline='')
                c.create_rectangle(cx + gap, h * 0.30, cx + gap + bw, h * 0.70,
                                   fill=color, outline='')
            elif c._kind == 'stop':
                side = w * 0.34
                c.create_rectangle(w / 2 - side / 2, h / 2 - side / 2,
                                   w / 2 + side / 2, h / 2 + side / 2,
                                   fill=color, outline='')

        c._redraw = redraw

        def on_enter(_):
            c._hover = True
            redraw()

        def on_leave(_):
            c._hover = False
            redraw()

        def on_click(_event):
            # 禁用的按钮不能触发动作 —— 只把图标压暗而仍然可点，
            # 用户会以为「点了没反应」是程序卡了。
            if getattr(c, '_enabled', True):
                command()
            return 'break'

        c.bind('<Enter>', on_enter)
        c.bind('<Leave>', on_leave)
        c.bind('<Button-1>', on_click)
        redraw()
        return c

    def _set_button(self, canvas: tk.Canvas, kind: str, enabled: bool):
        if canvas is None:
            return
        if getattr(canvas, '_kind', None) == kind and \
                getattr(canvas, '_enabled', None) == enabled:
            return
        canvas._kind = kind
        canvas._enabled = enabled
        canvas.configure(cursor='hand2' if enabled else 'arrow')
        canvas._redraw()

    def _bind_drag(self, widget):
        widget.bind('<Button-1>', self._drag_start)
        widget.bind('<B1-Motion>', self._drag_move)
        widget.bind('<ButtonRelease-1>', self._drag_end)

    # ------------------------------------------------------------ 拖动窗口
    def _drag_start(self, event):
        self._drag = (event.x_root, event.y_root,
                      self.root.winfo_x(), self.root.winfo_y())
        self._drag_moved = False

    def _drag_move(self, event):
        if not self._drag:
            return
        dx = event.x_root - self._drag[0]
        dy = event.y_root - self._drag[1]
        if abs(dx) > 2 or abs(dy) > 2:
            self._drag_moved = True
        self.root.geometry(f'+{self._drag[2] + dx}+{self._drag[3] + dy}')

    def _drag_end(self, _event):
        if self._drag and self._drag_moved:
            self._schedule_save_position()
        self._drag = None

    # ------------------------------------------------------------ 拖拽缩放
    def _resize_start(self, event):
        self._resize = (event.x_root, self.root.winfo_width())
        return 'break'

    def _resize_move(self, event):
        if not self._resize:
            return
        target = self._resize[1] + (event.x_root - self._resize[0])
        scale = target / (BASE_W * self._dpi)
        # 只有变化够大才重排，否则每一次鼠标移动都要重建一遍 font 对象
        if abs(scale - self.scale) > 0.03:
            self._apply_scale(scale)
        return 'break'

    def _resize_end(self, _event):
        if not self._resize:
            return
        self._resize = None
        try:
            self.backend.store.set('float_scale', round(self.scale, 3))
        except Exception:
            logging.debug('Could not save the floating bar scale', exc_info=True)
        return 'break'

    def _schedule_save_position(self):
        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
        # 防抖：连续拖动时只在停手 600ms 后写一次配置文件
        self._save_job = self.root.after(600, self._persist_position)

    def _persist_position(self):
        self._save_job = None
        try:
            self.backend.store.set(
                'float_pos', f'{self.root.winfo_x()},{self.root.winfo_y()}')
        except Exception:
            logging.debug('Could not save the floating bar position', exc_info=True)

    # ------------------------------------------------------------ 行为
    def _on_main(self):
        """主按钮：录制中→暂停，暂停中→继续，其它→开始录制。"""
        st = self.backend.status()
        op = st['op']
        if op['mode'] == 'record':
            if op['paused']:
                self.backend.submit(self.backend.resume_record)
            else:
                self.backend.submit(self.backend.pause_record)
        elif op['mode'] == 'export':
            return
        else:
            self.backend.submit(self.backend.toggle_record)

    def _on_stop(self):
        st = self.backend.status()
        if st['op']['mode'] in ('record', 'export'):
            self.backend.submit(self.backend.stop_operation)

    # ------------------------------------------------------------ 刷新
    def _tick(self):
        if self._closing:
            try:
                self.root.quit()
            except Exception:
                pass
            return
        try:
            self._render(self.backend.status())
        except Exception:
            logging.debug('Could not refresh the floating bar', exc_info=True)
        try:
            self.root.after(200, self._tick)
        except tk.TclError:
            pass

    def _render(self, st: dict):
        op = st['op']
        mode = op.get('mode') or 'idle'
        paused = bool(op.get('paused'))
        frames = int(op.get('frames') or 0)
        elapsed = float(op.get('elapsed') or 0)

        if mode == 'record':
            color, text = (AMBER, '已暂停') if paused else (RED, '录制中')
        elif mode == 'export':
            color, text = BLUE, '导出中'
        else:
            color, text = DIM, '未录制'

        self._dot.itemconfig(self._dot_id, fill=color)
        if self._status_lbl.cget('text') != text:
            self._status_lbl.configure(text=text)

        if mode == 'record':
            time_text = format_duration(elapsed)
            frame_text = f'{frames} 帧'
        elif mode == 'export':
            pct = int(round(float(op.get('progress') or 0) * 100))
            time_text = ''
            frame_text = f'{pct}%'
        else:
            time_text = ''
            frame_text = ''

        changed = False
        if self._time_lbl.cget('text') != time_text:
            self._time_lbl.configure(text=time_text)
            changed = True
        if self._frame_lbl.cget('text') != frame_text:
            self._frame_lbl.configure(text=frame_text)
            changed = True
        # 文字变了（空↔非空）就要重排：显示/隐藏时长和帧数由 _layout 决定
        if changed:
            self._layout(self.root.winfo_width(), self.root.winfo_height())

        # 主按钮：录制中显示暂停/继续，其余显示播放
        if mode == 'record':
            self._set_button(self._main_btn, 'play' if paused else 'pause', True)
        elif mode == 'export':
            self._set_button(self._main_btn, 'play', False)
        else:
            self._set_button(self._main_btn, 'play', True)

        self._set_button(self._stop_btn, 'stop', mode in ('record', 'export'))
