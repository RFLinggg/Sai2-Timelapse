# ---------------------------------------------------------------------------
# Modified from art-timelapse -- https://github.com/cromachina/art-timelapse
# Copyright (C) 2024-2026 cromachina. Licensed under GPL-3.0 (see LICENSE).
#
# 本文件在上游基础上做过修改：界面扩展：双界面切换、导出页素材统计、多语言
# 修改自 2026-11-19 起，逐次记录见 适配说明.md。
# ---------------------------------------------------------------------------
import asyncio
import ctypes
import logging
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
import json
from collections import deque
import os
import threading
import traceback
import time

import tkinter as tk
import ttkbootstrap as ttk
import ttkbootstrap.constants as ttkc
from ttkbootstrap.widgets import tooltip
from tkinter import filedialog, messagebox
from tkinter import ttk as stdttk
from PIL import Image, ImageTk
import cv2

from . import asynctk, timelapse, sai, set_locale, get_resource_path, get_log_path, _ as tr
from . import theme, ornaments, icons
from . import APP_NAME, APP_TITLE, SETTINGS_FILE, __version__, window_title
# 这两个和界面无关，WebView 版也要用，抽到 util.py 里共用
from .util import clamp_number, format_duration
from .store import migrate
from .hotkey import HotkeyListener

def _(t):
    return t

translatables = []

def get_fixed_font(font_name='Consolas', font_size=10):
    if font_name in tk.font.families():
        font = (font_name, font_size)
    else:
        if font_name not in tk.font.names():
            font_name = 'TkFixedFont'
        font = tk.font.nametofont(font_name)
        font.config(size=font_size)
    return font

class RollingAverage:
    def __init__(self, n=20):
        self.n = n
        self.total = 0
        self.values = deque()

    def put(self, value):
        self.total += value
        self.values.append(value)
        if len(self.values) > self.n:
            self.total -= self.values.popleft()

    def get(self):
        return self.total / len(self.values)

    def next(self, value):
        self.put(value)
        return self.get()

class DefaultGet:
    def get(self):
        try:
            return super().get()
        except:
            return self._default

class DefaultIntVar(DefaultGet, ttk.IntVar):
    pass

class DefaultDoubleVar(DefaultGet, ttk.DoubleVar):
    pass

class LocalizedStringVar(ttk.StringVar):
    def __init__(self, text, *args, **kwargs):
        super().__init__(*args, **kwargs)
        translatables.append(self)
        self.set(text)

    def set(self, text):
        self.init_text = text
        self.update_translation()

    def update_translation(self):
        # gettext 把空 msgid 当作 .po 的元数据条目，tr('') 会把整段
        # "Project-Id-Version: ..." 头信息返回回来。空串必须原样保留 ——
        # 否则「检测到的 SAI 版本」在没检测到时会显示成 PO 文件头。
        text = self.init_text
        super().set(tr(text) if text else text)

class StackStringVar(ttk.StringVar):
    def __init__(self, value, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stack = []
        self.cbname = None
        self.linked_var = None
        self.push(value)

    def unset_value(self):
        if self.linked_var is not None:
            self.linked_var.trace_remove('write', self.cbname)
            self.linked_var = None
            self.cbname = None

    def set_value(self, value):
        self.unset_value()
        if isinstance(value, ttk.StringVar):
            self.cbname = value.trace_add('write', lambda *_: self.set(value.get()))
            self.linked_var = value
            self.set(value.get())
        else:
            self.set(value)

    def push(self, value):
        self.set_value(value)
        self.stack.append(value)

    def pop(self):
        value = self.stack.pop()
        if self.stack:
            self.set_value(self.stack[-1])
        else:
            self.set_value('')

class ButtonRow(ttk.Frame):
    def __init__(self, master, text=None, textvariable=None, style='Accent.TButton', icon=None):
        super().__init__(master)
        self.pack(fill=ttkc.X, pady=(6, 0))
        self.button = stdttk.Button(self, text=text, textvariable=textvariable, style=style)
        self.button.pack(side=ttkc.LEFT, fill=ttkc.X, expand=True, ipady=3)
        self._icon_name = icon
        if icon:
            self.set_icon(icon)

    def _icon_token(self):
        """图标颜色跟文字走：强调色按钮上是深字，普通按钮上是浅字。"""
        style = str(self.button.cget('style'))
        return {
            'Accent.TButton': 'accent_fg',
            'Danger.TButton': 'danger_text',
        }.get(style, 'text')

    def set_icon(self, name):
        self._icon_name = name
        if not name:
            self.button.config(image='')
            return
        photo = icons.make(name, self._icon_token())
        if photo is not None:
            self.button.config(image=photo, compound=ttkc.LEFT)

    def set_text(self, text):
        self.button.config(text=text)

    def set_callback(self, callback):
        self.button.config(command=callback)

class ProgressRow(ttk.Frame):
    def __init__(self, master, font=None):
        super().__init__(master)
        self.pack(fill=ttkc.X)
        self.label = ttk.Label(self, font=font)
        self.label.pack()
        self.progressbar = ttk.Progressbar(self)
        self.progressbar.pack(fill=ttkc.X, expand=True)
        self.loop = asyncio.get_running_loop()

    def iterate(self, iterable, total, unit='it'):
        self.last_update = float('-inf')
        self.avg_ips = RollingAverage()
        self.avg_eta_sec = RollingAverage()
        i = 0
        start_time = time.monotonic()
        delta = 0
        for it in iterable:
            delta_start = time.monotonic()
            yield it
            i += 1
            delta = time.monotonic() - delta_start
            self.loop.call_soon_threadsafe(self.set_value, i, total, unit, start_time, delta)
        self.last_update = float('-inf')
        self.loop.call_soon_threadsafe(self.set_value, i, total, unit, start_time, delta)

    def set_value(self, i, total, unit, start_time, delta):
        value = (i / total) * 100
        now = time.monotonic()
        if now - self.last_update > 0.1:
            self.last_update = now
            elapsed_time = time.monotonic() - start_time
            elapsed_minutes = int(elapsed_time // 60)
            elapsed_seconds = int(elapsed_time % 60)
            eta_total = delta * (total - i)
            eta_minutes = int(eta_total // 60)
            eta_seconds = int(self.avg_eta_sec.next(eta_total % 60))
            ips = self.avg_ips.next(1 / delta)
            self.label.config(text=f'{i}/{total} [{elapsed_minutes:02}:{elapsed_seconds:02}<{eta_minutes:02}:{eta_seconds:02}, {ips:6.2f}{unit}/s]')
        self.progressbar.config(value=value)

class LabelRow(ttk.Frame):
    def __init__(self, master, label):
        super().__init__(master)
        self.pack(fill=ttkc.X, pady=(0, 2))
        # 标签是次要信息：小一号、灰一档，让值（输入框里的内容）先被看到
        self.label = stdttk.Label(self, text=label, textvariable=LocalizedStringVar(label),
                                  style='RowLabel.TLabel')
        self.label.pack(side=ttkc.LEFT)

class StatusLabelRow(LabelRow):
    def __init__(self, master, label_text, status_text='', textvariable=None):
        super().__init__(master, label_text)
        self.status = ttk.Entry(self, text=status_text, textvariable=textvariable,
                                state=ttkc.READONLY, style='Readonly.TEntry')
        self.status.pack(side=ttkc.LEFT, fill=ttkc.X, expand=True)

class ImageLabelRow(LabelRow):
    def __init__(self, master, label):
        super().__init__(master, label)
        # 1px 描边：ttk 的 Frame 不能直接设边框色，垫一层 tk.Frame
        border = flat_tk(tk.Frame, self, 'border')
        border.pack(side=ttkc.LEFT, padx=(0, 0))
        self.canvas = flat_tk(tk.Canvas, border, 'sunken', height=92, width=120,
                              highlightthickness=0, borderwidth=0)
        self.canvas.pack(padx=1, pady=1)
        self.image_tag = None
        self.draw_empty()

    def draw_empty(self):
        """空状态：一枚铃铛 + 一行灰字。这是全屏唯一的大装饰。

        画布 120×92，铃铛按 24 视图框里 x=12 是中心线换算左边界，
        保证铃铛和下方文字都落在画布中轴上。
        """
        self.canvas.delete('empty')
        size = 36
        cx = 60
        left = cx - size / 2.0
        ornaments.draw_bell(self.canvas, size, theme.color('border_strong'), left, 8)
        self.canvas.create_text(cx, 64, text=tr(_('No preview')),
                                fill=theme.color('text_faint'),
                                font=theme.font(9), tags='empty')
        self.canvas.addtag_withtag('empty', 'all')

    def set_image(self, image):
        self.canvas.delete('empty')
        self.image = ImageTk.PhotoImage(image)
        if self.image_tag is None:
            self.image_tag = self.canvas.create_image(50, 50, anchor=ttkc.CENTER, image=self.image)
        self.canvas.itemconfig(self.image_tag, image=self.image)

    def remove_image(self):
        if self.image_tag is not None:
            self.canvas.delete(self.image_tag)
            self.image_tag = None
        self.draw_empty()

class CheckbuttonLabelRow(LabelRow):
    def __init__(self, master, label, variable=None):
        super().__init__(master, label)
        self.checkbutton = ttk.Checkbutton(self, variable=variable)
        self.checkbutton.pack(side=ttkc.LEFT)

class EntryLabelRow(LabelRow):
    def __init__(self, master, label, number_type=None, button_label=None, textvariable=None,
                 button_icon=None):
        super().__init__(master, label)
        self.textvariable = textvariable or ttk.StringVar()
        kwargs = {}
        if number_type is None:
            if isinstance(self.textvariable, ttk.IntVar):
                number_type = int
            elif isinstance(self.textvariable, ttk.DoubleVar):
                number_type = float
        if number_type:
            def check_number(p):
                if str(p) == '':
                    return True
                try:
                    number_type(p)
                    return True
                except:
                    return False
            kwargs['validate'] = ttkc.ALL
            proc = self.register(check_number)
            kwargs['validatecommand'] = (proc, '%P')
        kwargs['textvariable'] = self.textvariable
        self.entry = ttk.Entry(self, **kwargs)
        self.entry.pack(side=ttkc.LEFT, fill=ttkc.X, expand=True)
        self.button = None
        if button_label is not None:
            self.button = stdttk.Button(self, text=button_label, width=10,
                                        textvariable=LocalizedStringVar(button_label))
            self.button._icon_name = button_icon
            self._apply_button_icon()
            self.button.pack(side=ttkc.LEFT, padx=(8, 0))

    def _apply_button_icon(self):
        name = getattr(self.button, '_icon_name', None)
        if not name:
            return
        photo = icons.make(name, 'text')
        if photo is not None:
            self.button.config(image=photo, compound=ttkc.LEFT)

    def enable(self, state):
        self.entry.config(state=ttkc.NORMAL if state else ttkc.DISABLED)

def get_nearest_dir(path):
    path = timelapse.expand_path(path).absolute()
    if path.is_dir():
        return path
    for p in path.parents:
        if p.is_dir():
            return p
    return Path('.')

class FilePickerEntry(EntryLabelRow):
    def __init__(self, master, text, title=_('Select'), mode='open', filetypes=((_('Any'), '*.*'),), button_label=_('Select'), **kwargs):
        super().__init__(master, text, button_label=button_label, button_icon='folder', **kwargs)
        self.title = title
        self.mode = mode
        self.filetypes = filetypes
        self.button.config(command=self.pick_file)

    def pick_file(self, *_args):
        initdir = get_nearest_dir(self.textvariable.get())
        title = tr(self.title)
        filetypes = [tuple((tr(x) for x in ftype)) for ftype in self.filetypes]
        filename = None
        match self.mode:
            case 'open':
                filename = filedialog.askopenfilename(
                    title=title,
                    initialdir=initdir,
                    filetypes=filetypes,
                )
            case 'save':
                filename = filedialog.asksaveasfilename(
                    title=title,
                    initialdir=initdir,
                    filetypes=filetypes,
                )
            case 'dir':
                filename = filedialog.askdirectory(
                    title=title,
                    initialdir=initdir,
                )
            case _:
                return
        if filename is not None and filename:
            self.textvariable.set(filename)

class ComboboxLabelRow(LabelRow):
    def __init__(self, master, text, values=None, index_variable=None):
        super().__init__(master, text)
        self.index_var = index_variable
        if self.index_var is None:
            self.index_var = ttk.IntVar(value=0)
        self.combobox = stdttk.Combobox(self, state=ttkc.READONLY)
        self.combobox.pack(side=ttkc.LEFT, fill=ttkc.X, expand=True)
        self.reentering = False
        self.combobox.bind('<<ComboboxSelected>>', self.on_selected)
        self.index_var.trace_add('write', self.var_trace)
        self.values = values
        if values is not None:
            self.set_values(values)

    def var_trace(self, *_args):
        if self.reentering:
            return
        if isinstance(self.index_var, ttk.StringVar):
            self.combobox.set(self.index_var.get())
        else:
            self.combobox.current(self.index_var.get())

    def on_selected(self, _event):
        self.reentering = True
        if isinstance(self.index_var, ttk.StringVar):
            self.index_var.set(self.combobox.get())
        else:
            self.index_var.set(self.combobox.current())
        self.reentering = False

    def get_index(self):
        return self.combobox.current()

    def set_values(self, values):
        self.combobox.config(values=[value.get() if isinstance(value, ttk.StringVar) else value for value in values])
        self.values = values
        try:
            if isinstance(self.index_var, ttk.StringVar):
                self.combobox.current(values.index(self.index_var.get()))
            else:
                self.combobox.current(self.index_var.get())
        except:
            if len(values) > 0:
                self.combobox.current(0)
            else:
                self.combobox.set('')

    def get_values(self):
        return self.values

class ToolTip(tooltip.ToolTip):
    def __init__(self, widget, text, *args, **kwargs):
        super().__init__(widget, *args, text=text, **kwargs)
        self.textvariable = LocalizedStringVar(text)
        self.textvariable.trace_add('write', self.update_text)
        self.update_text()

    def update_text(self, *_args):
        self.text = self.textvariable.get()

def add_tooltips(widget, text):
    if not isinstance(widget, ttk.Frame):
        ToolTip(widget, text)
    else:
        for child in widget.winfo_children():
            add_tooltips(child, text)

class AdvancedPanel(ttk.Frame):
    """可展开的高级设置区（折叠面板，不是开关），默认收起。

    整行标题可点：左侧 ▸/▾ 指示状态，点一下展开、再点收起。
    比原来的圆角开关更像「一节内容」而不是「一个选项」——
    高级区里放的是好几行设置，用开关暗示"开关某功能"是错的。
    收起状态下控件仍然存在（只是没有 pack），所以变量、回调、翻译都不受影响。
    """

    CHEVRON = 20   # 箭头画布边长，和状态区铃铛的 20px 画布对齐
    PAD_Y = 6      # 标题行上下留白

    def __init__(self, master, title=None):
        super().__init__(master)
        self.pack(fill=ttkc.X, pady=(8, 0))
        ttk.Separator(self, orient=ttkc.HORIZONTAL).pack(fill=ttkc.X, pady=(0, 2))
        # 由外部（ArtTimelapseApp）挂上，展开/收起后需要重算窗口高度。
        self.on_toggle = None
        self.title_var = LocalizedStringVar(title or _('Advanced settings'))
        # 默认收起；ART_TIMELAPSE_ADV_OPEN=1 时启动即展开（用于截图/自检）。
        default_open = os.environ.get('ART_TIMELAPSE_ADV_OPEN') == '1'
        self.open_var = ttk.BooleanVar(value=default_open)
        self._hover = False

        # 整行做成可点区域。highlightthickness 顺带当键盘焦点框用：
        # 平时是窗口色（看不见），拿到焦点后变强调色。
        self.header = flat_tk(
            tk.Frame, self, 'window', takefocus=True,
            highlightthickness=1,
            highlightbackground=theme.color('window'),
            highlightcolor=theme.color('accent'),
            cursor='hand2',
        )
        self.header.pack(fill=ttkc.X)

        # 箭头用两条线画，不依赖字体里有没有 ▸/▾ 这两个字形。
        # 画布 20px、左内距 9px：和状态区那个铃铛的左边缘对在同一条竖线上。
        self.chevron = tk.Canvas(self.header, width=self.CHEVRON, height=self.CHEVRON,
                                 highlightthickness=0, bd=0,
                                 background=theme.color('window'))
        self.chevron._tb_no_autostyle = True
        self.chevron.pack(side=ttkc.LEFT, padx=(9, 2), pady=3)

        self.label = flat_tk(tk.Label, self.header, 'window',
                             textvariable=self.title_var, font=theme.font(10),
                             foreground=theme.color('text_muted'),
                             anchor='w', bd=0, cursor='hand2')
        self.label.pack(side=ttkc.LEFT, fill=ttkc.X, expand=True, pady=self.PAD_Y)

        for widget in (self.header, self.chevron, self.label):
            widget.bind('<Button-1>', self._on_click)
            widget.bind('<Enter>', self._on_enter)
            widget.bind('<Leave>', self._on_leave)
        self.header.bind('<space>', self._on_click)
        self.header.bind('<Return>', self._on_click)

        self.body = ttk.Frame(self, style='Panel.TFrame')
        self._apply_state()

    # ------------------------------------------------------------ 交互
    def _on_click(self, _event=None):
        self.set_open(not self.open_var.get())
        return 'break'

    def _on_enter(self, _event=None):
        if not self._hover:
            self._hover = True
            self._update_colors()

    def _on_leave(self, _event=None):
        # 指针从标题行移到子控件上时，父控件同样会收到 Leave。
        # 用指针的实际位置判断是不是真的离开了整行，否则整行会一直闪。
        if not self._hover:
            return
        px, py = self.header.winfo_pointerxy()
        x, y = self.header.winfo_rootx(), self.header.winfo_rooty()
        if (x <= px < x + self.header.winfo_width() and
                y <= py < y + self.header.winfo_height()):
            return
        self._hover = False
        self._update_colors()

    def _update_colors(self):
        bg = 'hover' if self._hover else 'window'
        fg = 'text' if self._hover else 'text_muted'
        for widget in (self.header, self.label):
            # repaint_flat_tk() 按这个令牌重刷，悬停态才不会在切主题时被抹掉
            widget._flat_token = bg
            widget.config(background=theme.color(bg))
        self.label.config(foreground=theme.color(fg))
        self.chevron.config(background=theme.color(bg))
        self._draw_chevron()

    def _draw_chevron(self):
        c = self.chevron
        c.delete('all')
        # 在 20px 画布里居中画一个约 6px 的箭头
        if self.open_var.get():
            points = (7, 8.5, 10, 11.5, 13, 8.5)     # ▾ 展开
        else:
            points = (8.5, 7, 11.5, 10, 8.5, 13)     # ▸ 收起
        c.create_line(*points,
                      fill=theme.color('text' if self._hover else 'text_muted'),
                      width=1.6, capstyle=ttkc.ROUND, joinstyle=ttkc.ROUND,
                      tags='chevron')

    def refresh(self):
        """切主题后重刷 —— 箭头和标题色是直接写进控件的，不跟样式表走。"""
        self._update_colors()
        self.header.config(highlightbackground=theme.color('window'),
                           highlightcolor=theme.color('accent'))

    # ------------------------------------------------------------ 状态
    def _apply_state(self):
        if self.open_var.get():
            self.body.pack(fill=ttkc.X)
        else:
            self.body.pack_forget()
        self._draw_chevron()
        on_toggle = getattr(self, 'on_toggle', None)
        if on_toggle is not None:
            on_toggle()

    def set_open(self, value: bool):
        self.open_var.set(bool(value))
        self._apply_state()

class VideoConfigFrame(ttk.Frame):
    def __init__(self, master, shared_vars, advanced_master=None, icon=None):
        translatables.append(self)
        super().__init__(master)
        self.pack(fill=ttkc.X)
        self.video_types = [
            (_('mp4/avc1 - Works with most websites, including Twitter'), ('mp4', 'avc1')),
            (_('webm/vp80 - Better color quality, but larger file size'), ('webm', 'vp80')),
            (_('Use custom container/codec'), None)
        ]
        self.video_type_var = shared_vars['video_type_var']
        self.custom_container_var = shared_vars['custom_container_var']
        self.custom_codec_var = shared_vars['custom_codec_var']
        self.button_text = shared_vars['button_text']
        # 格式选项属于「高级」，按钮留在主区域。
        target = self if advanced_master is None else advanced_master
        self.video_type_box = ComboboxLabelRow(target, _('Video type'), values=[t[0] for t in self.video_types], index_variable=self.video_type_var)
        add_tooltips(self.video_type_box, _('Useful container/codec presets.'))
        self.custom_container_entry = EntryLabelRow(target, _('Custom container'), textvariable=self.custom_container_var)
        add_tooltips(self.custom_container_entry, _('If you want to use a container that is not listed above, whatever is supported by OpenCV and FFMPEG.'))
        self.custom_codec_entry = EntryLabelRow(target, _('Custom codec'), textvariable=self.custom_codec_var)
        add_tooltips(self.custom_codec_entry, _('If you want to use a codec that is not listed above, whatever is supported by OpenCV and FFMPEG.'))
        self.button = ButtonRow(self, textvariable=self.button_text, icon=icon)

    def get_format(self):
        video_format = self.video_types[self.video_type_var.get()][1]
        if video_format is None:
            video_format = self.custom_container_var.get() or 'mp4', self.custom_codec_var.get() or 'avc1'
        return video_format

    def set_button_callback(self, callback):
        self.button.set_callback(callback)

    def update_translation(self):
        values = [tr(vtype[0]) for vtype in self.video_types]
        self.video_type_box.set_values(values)

class StatusArea(ttk.Frame):
    # 日志超过这个行数就从顶部截掉一批，避免长时间挂机把内存吃满
    MAX_LINES = 3000

    def __init__(self, master, font=None):
        super().__init__(master)
        self.pack(fill=ttkc.BOTH, expand=True, padx=12, pady=(0, 10))

        # 标题行：铃铛（通知语义）+ 标题 + 右侧工具条
        header = ttk.Frame(self)
        header.pack(fill=ttkc.X, pady=(0, 4))
        self.bell = ornaments.Ornament(header, kind='bell', size=16, token='text_muted')
        self.bell.pack(side=ttkc.LEFT)
        self._title_var = LocalizedStringVar(_('Status'))
        self.title_label = stdttk.Label(header, textvariable=self._title_var, style='Section.TLabel')
        self.title_label.pack(side=ttkc.LEFT, padx=(4, 0))

        self.clear_text = LocalizedStringVar(_('Clear'))
        self.copy_text = LocalizedStringVar(_('Copy'))
        self.open_log_text = LocalizedStringVar(_('Open log file'))
        stdttk.Button(header, textvariable=self.open_log_text, style='Ghost.TButton',
                   command=self.open_log_file).pack(side=ttkc.RIGHT, padx=(4, 0))
        stdttk.Button(header, textvariable=self.copy_text, style='Ghost.TButton',
                   command=self.copy_all).pack(side=ttkc.RIGHT, padx=(4, 0))
        stdttk.Button(header, textvariable=self.clear_text, style='Ghost.TButton',
                   command=self.clear).pack(side=ttkc.RIGHT)

        # 1px 描边靠外层 Frame 垫 1px 实现 —— ttk 的 Frame 不能直接设边框色
        border = flat_tk(tk.Frame, self, 'border')
        border.pack(fill=ttkc.BOTH, expand=True)
        # 不用 ttkbootstrap 的 ScrolledText：它内部还套一层 bootstyle='highlight'
        # 的容器，那个样式在自定义主题里没定义，会退回系统面板色 #F0F0F0 ——
        # 日志区四周那圈白边就是它露出来的 padding。自己拼 Text + 滚动条，
        # 每一层颜色都归我们管。
        self.text = tk.Text(border, font=font, width=1, height=1,
                            background=theme.color('sunken'),
                            foreground=theme.color('text_muted'),
                            insertbackground=theme.color('text'),
                            selectbackground=theme.color('selection'),
                            borderwidth=0, highlightthickness=0,
                            padx=10, pady=8, state=ttkc.DISABLED)
        self.vbar = stdttk.Scrollbar(border, orient=ttkc.VERTICAL,
                                     style='Vertical.TScrollbar',
                                     command=self.text.yview)
        self.text.configure(yscrollcommand=self.vbar.set)
        self.text.grid(row=0, column=0, sticky=ttkc.NSEW, padx=(1, 0), pady=1)
        self.vbar.grid(row=0, column=1, sticky=ttkc.NS, padx=(0, 1), pady=1)
        border.grid_rowconfigure(0, weight=1)
        border.grid_columnconfigure(0, weight=1)
        self._apply_tags()

    def _apply_tags(self):
        self.text.tag_configure('notice', foreground=theme.color('accent'))
        self.text.tag_configure('error', foreground=theme.color('danger_text'))
        self.text.tag_configure('muted', foreground=theme.color('text_muted'))

    def append(self, text, level='info'):
        # 窗口关掉之后日志 handler 还挂在 root logger 上，回调会继续来。
        # 不拦的话每行都抛 TclError，日志文件被刷满。
        if not self.text.winfo_exists():
            return
        tag = {'notice': 'notice', 'error': 'error'}.get(level, 'muted')
        self.text.config(state=ttkc.NORMAL)
        self.text.insert(ttkc.END, f'{text}\n', tag)
        lines = int(self.text.index('end-1c').split('.')[0])
        if lines > self.MAX_LINES:
            self.text.delete('1.0', f'{lines - self.MAX_LINES + 500}.0')
        self.text.see(ttkc.END)
        self.text.config(state=ttkc.DISABLED)

    def clear(self):
        self.text.config(state=ttkc.NORMAL)
        self.text.delete('1.0', ttkc.END)
        self.text.config(state=ttkc.DISABLED)

    def copy_all(self):
        content = self.text.get('1.0', ttkc.END).strip()
        if not content:
            return
        self.clipboard_clear()
        self.clipboard_append(content)

    def open_log_file(self):
        path = get_log_path()
        if path is None or not path.exists():
            messagebox.showinfo(title=tr(_('Status')),
                                message=tr(_('No log file yet. Logs are written to a file only in the packaged build.')),
                                parent=self)
            return
        try:
            os.startfile(str(path))
        except Exception:
            logging.exception('Could not open log file')
            messagebox.showinfo(title=tr(_('Status')), message=str(path), parent=self)

class AsyncWidgetLogger(logging.Handler):
    def __init__(self, widget):
        super().__init__()
        self.setLevel(logging.INFO)
        self.setFormatter(logging.Formatter('[%(asctime)s][%(levelname)s] %(message)s'))
        self.widget = widget
        self.loop = asyncio.get_running_loop()

    def emit(self, record):
        # 用颜色区分严重程度，比在每行前面贴图标更安静
        level = 'error' if record.levelno >= logging.ERROR else (
            'notice' if record.levelno >= logging.WARNING else 'info')
        self.loop.call_soon_threadsafe(self.widget.append, self.format(record), level)

def make_frames_path_field(master, textvariable=None):
    frames_path = FilePickerEntry(master, _('Save folder'), mode='dir', textvariable=textvariable)
    add_tooltips(frames_path, _('Folder where video recording cuts are saved.'))

def make_frames_path_readonly(master, textvariable=None, on_open=None):
    """导出页只读展示帧目录 —— 它和录制页是同一个值，做成第二个输入框只会让人以为要分别设。"""
    row = StatusLabelRow(master, _('Save folder'), textvariable=textvariable)
    if on_open is not None:
        open_text = LocalizedStringVar(_('Open'))
        button = stdttk.Button(row, textvariable=open_text, width=8, command=on_open)
        button.pack(side=ttkc.LEFT, padx=(6, 0))
    return row

def make_auto_split_field(master, textvariable=None):
    auto_split = EntryLabelRow(master, _('Auto split count'), textvariable=textvariable)
    add_tooltips(auto_split, _('Automatically split video output after this many frames. Enter 0 or leave blank to disable. This is useful to mitigate the chance of missing or corrupt video data after a system crash.'))

class ImageSizeRow(LabelRow):
    """录制分辨率上限：预设下拉，也能直接填像素。

    设置里存的仍是像素数（沿用 ``image_size_limit``），但显示成 1080p 之类 ——
    「1000」这种裸数字看不出含义，也没法判断它影响的是成片清晰度还是体积。
    """

    PRESETS = [
        (1280, '720p'),
        (1920, '1080p'),
        (2560, '1440p'),
        (3840, '4K'),
        (16384, None),   # None = 原始尺寸，显示时不带 px
    ]

    def __init__(self, master, textvariable):
        translatables.append(self)
        super().__init__(master, _('Capture size limit'))
        self.var = textvariable
        self.original_label = LocalizedStringVar(_('Original size'))
        self.combo = stdttk.Combobox(self, width=1)
        self.combo.pack(side=ttkc.LEFT, fill=ttkc.X, expand=True)
        self.combo.bind('<<ComboboxSelected>>', self.on_selected)
        self.combo.bind('<Return>', self.on_typed)
        self.combo.bind('<FocusOut>', self.on_typed)
        self.var.trace_add('write', self.refresh)
        self.update_translation()

    def preset_text(self, px:int) -> str:
        for value, label in self.PRESETS:
            if value == px:
                return f'{label} ({px} px)' if label else self.original_label.get()
        return f'{px} px'

    def update_translation(self):
        if not hasattr(self, 'combo'):
            return
        self.combo.config(values=[self.preset_text(value) for value, _label in self.PRESETS])
        self.refresh()

    def refresh(self, *_args):
        try:
            px = int(self.var.get())
        except Exception:
            px = 1920
        text = self.preset_text(px)
        if self.combo.get() != text:
            self.combo.set(text)

    def on_selected(self, *_args):
        text = self.combo.get()
        for value, label in self.PRESETS:
            if text == (f'{label} ({value} px)' if label else self.original_label.get()):
                self.var.set(value)
                return

    def on_typed(self, *_args):
        digits = ''.join(ch for ch in self.combo.get() if ch.isdigit())
        if digits:
            self.var.set(int(clamp_number(digits, 100, 16384, 1920)))
        self.refresh()

def make_notebook_frame(notebook:ttk.Notebook, text:str):
    frame = ttk.Frame(notebook)
    frame.pack(fill=ttkc.BOTH, expand=True)
    notebook.add(frame, text=text)
    var = LocalizedStringVar(text)
    tab_id = notebook.index('end') - 1
    var.trace_add('write', lambda *_args: notebook.tab(tab_id, text=var.get()))
    frame._tab_name_var = var
    return frame

def get_widgets_by_type(master, wtype):
    for widget in master.winfo_children():
        if isinstance(widget, wtype):
            yield widget
        yield from get_widgets_by_type(widget, wtype)

# 手绘/自定义底色的原生 tk 控件登记表：[(widget, token_name), ...]
# ttkbootstrap 2.x 劫持了 tk.Frame / tk.Canvas 的构造函数，建完立刻按主题把
# background 刷成窗口色（style/builders_tk.py 的 update_frame_style），
# 传进去的 background 会被吃掉 —— 1px 描边因此整条消失。
# 这里统一用 autostyle=False 退出那套自动着色，颜色改由 refresh_ornaments()
# 按设计令牌重刷，切主题时才不会掉色。
_FLAT_TK = []


def flat_tk(cls, master, token, **kwargs):
    widget = cls(master, background=theme.color(token), **kwargs)
    # 引擎遍历（StyleEngine._walk_and_repaint）认这个标记，打了就不再重涂。
    widget._tb_no_autostyle = True
    widget._flat_token = token
    _FLAT_TK.append(widget)
    return widget


def repaint_flat_tk():
    """按当前令牌重刷所有手绘 tk 控件的底色，顺手清掉已销毁的。"""
    for widget in list(_FLAT_TK):
        try:
            if not widget.winfo_exists():
                _FLAT_TK.remove(widget)
                continue
            widget.config(background=theme.color(widget._flat_token))
        except Exception:
            logging.debug('Could not repaint %s', widget, exc_info=True)

def get_text_width(label):
    return tk.font.Font(font=label['font']).measure(label['text'])

def auto_size_label_rows(master):
    label_rows = list(get_widgets_by_type(master, LabelRow))
    max_width = 0
    for row in label_rows:
        row.label.config(width=0)
        max_width = max(max_width, row.label.winfo_reqwidth())
    for row in label_rows:
        row.label.pack(side=ttkc.LEFT, ipadx=max_width/2, ipady=9, padx=5, fill=ttkc.X)
        row.label.config(width=1)

class Settings:
    var_types = {
        int: DefaultIntVar,
        str: ttk.StringVar,
        bool: ttk.BooleanVar,
        float: DefaultDoubleVar,
    }

    def __init__(self, file_name):
        self.file_path = Path.home() / file_name
        self.data = {}
        self.vars = {}
        # get_var 时记下默认值，「恢复默认设置」才有依据 —— 否则默认值散落在
        # 几十处调用点上，重置时只能靠删文件。
        self.defaults = {}
        # 载入阶段传进来的「默认值」其实是已保存的值，不能当默认值记下来
        self._loading = True
        if self.file_path.exists():
            # Explicit UTF-8: without it, open() falls back to the ANSI code
            # page (cp936 here) when the app is launched from Explorer, which
            # garbles the Chinese paths stored in the settings file.
            with self.file_path.open(encoding='utf-8') as fp:
                self.data = json.load(fp)
                for k, v in self.data.items():
                    self.get_var(k, v)
        self._loading = False
        # 旧默认值 → 新默认值。规则和 WebView 版共用 store.migrate，
        # 两边各写一份迟早会走岔。
        changes = migrate(self.data)
        if changes is not None:
            for key, value in changes.items():
                var = self.vars.get(key)
                if var is not None:
                    var.set(value)   # 变量的 trace 会顺手同步回 self.data
            # 立刻落盘：只在正常退出时保存的话，强杀/崩溃后会反复重跑，
            # 用户手动改回旧默认值也会被再改一次。
            self.save()

    def get_var(self, key, default):
        if not self._loading:
            self.defaults.setdefault(key, default)
        if key in self.vars:
            var = self.vars.get(key)
            if type(var.get()) == type(default):
                return var
            else:
                var = Settings.var_types.get(type(default), ttk.Variable)(value=var.get())
        else:
            var = Settings.var_types.get(type(default), ttk.Variable)(value=default)
        def save(*_args):
            self.data[key] = var.get()
        var.trace_add('write', save)
        self.vars[key] = var
        return var

    def save(self):
        with self.file_path.open('w', encoding='utf-8') as fp:
            json.dump(self.data, fp, ensure_ascii=False)

    def reset(self, keep=()):
        """把设置恢复成默认值，保留 keep 里的键（帧目录、主题、语言、窗口位置）。

        变量是活的，改完界面会立刻跟着变，不用重启。
        """
        restored = []
        for key, var in list(self.vars.items()):
            if key in keep:
                continue
            default = self.defaults.get(key)
            if default is None:
                continue
            try:
                var.set(default)
                restored.append(key)
            except Exception:
                logging.debug('Could not reset setting %s', key, exc_info=True)
        self.save()
        return restored

    def __enter__(self):
        return self

    def __exit__(self, *exc_args):
        self.save()

class App(asynctk.AsyncTk):
    def __init__(self, *args, **kwargs):
        # className 用不带 emoji 的 APP_NAME：它是 Tk 的窗口类名，
        # 会被塞进注册表和任务栏，emoji 在这里没有意义还容易出编码岔子。
        super().__init__(*args, className=APP_NAME, **kwargs)
        self.base_title = window_title()
        self.title(self.base_title)
        try:
            icon_path = get_resource_path(f'{APP_NAME}.ico')
            if icon_path.exists():
                self.iconbitmap(str(icon_path))
        except Exception:
            logging.debug('Window icon could not be applied', exc_info=True)
        # 配置文件仍叫 .art-timelapse，和 APP_NAME 无关 —— 见 __init__.py 的说明
        self.settings = Settings(SETTINGS_FILE)
        # 必须在 theme_use 之前注册：ttkbootstrap 只认识已注册的主题名
        theme.register(self.style)
        # 窗口 map 之后 DWM 才会接受标题栏配色
        self.after(80, self.theme_titlebar)
        self.after(85, self.apply_window_background)
        self.after(90, self.style_popdowns)
        fixed_font = get_fixed_font()

        #########################################################
        # Tk variables
        self.win_size_var = self.settings.get_var('win_size', (600, 600))
        self.frames_file_var = self.settings.get_var('frames_file', '')
        self.auto_split_sai_var = self.settings.get_var('auto_split_sai', 500)
        self.recording_button_text_var = StackStringVar(value=LocalizedStringVar(_('Start Recording')))
        self.export_button_text_var = StackStringVar(value=LocalizedStringVar(_('Export')))
        recording_vars = {
            'video_type_var': self.settings.get_var('recording_type', 0),
            'custom_container_var': self.settings.get_var('recording_custom_container', ''),
            'custom_codec_var': self.settings.get_var('recording_custom_codec', ''),
            'button_text': self.recording_button_text_var,
        }
        export_vars = {
            'video_type_var': self.settings.get_var('export_type', 0),
            'custom_container_var': self.settings.get_var('export_custom_container', ''),
            'custom_codec_var': self.settings.get_var('export_custom_codec', ''),
            'button_text': self.export_button_text_var,
        }
        self.sai_version_status_var = LocalizedStringVar('')
        self.sai_version_override_var = self.settings.get_var('sai_version_override', 0)
        self.sai_canvas_var = self.settings.get_var('sai_canvas', '')
        self.image_size_limit_var = self.settings.get_var('image_size_limit', 1920)
        # 导出目录已经并进「帧目录」了：成片就落在会话子目录旁边。
        # 老配置里可能还留着 export_dir / export_file_var，读都别读，免得又冒出一个框。
        self.settings.data.pop('export_dir', None)
        self.settings.data.pop('export_file_var', None)
        self.export_result_var = ttk.StringVar(value='')
        self._recording_started_at = 0.0
        # 旧版默认 60 秒，等于「录 3 小时只出 60 秒」，界面还不提示。
        # 新版默认 0（不限）；仍停在旧默认值上的配置由 Settings.__init__ 里的
        # store.migrate 统一迁走，这里只负责建变量。
        self.export_time_limit_var = self.settings.get_var('export_time_limit', 0.0)
        self.export_fps_var = self.settings.get_var('export_fps', 30.0)
        self.export_preview_last_frame_first_var = self.settings.get_var('export_preview_last_frame', True)
        self.export_preview_duration = self.settings.get_var('export_preview_duration', 0.0)
        self.export_use_recording_var = self.settings.get_var('export_user_recording', True)
        self.export_summary_var = ttk.StringVar(value='')
        # 预估任务：帧目录/帧率/时长上限一变就重扫，做 300ms 防抖
        self._summary_after = None
        self._summary_task = None
        self.selected_tab_var = self.settings.get_var('selected_tab', 0)
        self.selected_tab_thread_safe = 0
        meta_config_theme_var = self.settings.get_var('theme', theme.DEFAULT_THEME)
        meta_config_lang_var = self.settings.get_var('lang', 'zh_CN')

        # 录制中才出现的红点徽标（标题栏被 SAI 挡住时的第二处状态提示）
        self.recording_badge = None
        self.recording_dot = None

        #########################################################
        # GUI widgets and layout
        notebook = ttk.Notebook(self)
        notebook.pack(side=ttkc.TOP, fill=ttkc.X)

        sai_frame = make_notebook_frame(notebook, _('SAI Recording'))
        sai_main = ttk.Frame(sai_frame)
        sai_main.pack(fill=ttkc.X)
        self.sai_advanced = AdvancedPanel(sai_frame)

        self.recording_badge = self.make_recording_badge(sai_main)
        make_frames_path_field(sai_main, self.frames_file_var)
        self.sai_canvas_box = ComboboxLabelRow(sai_main, _('Canvas'), index_variable=self.sai_canvas_var)
        StatusLabelRow(sai_main, _('SAI version detected'), textvariable=self.sai_version_status_var)
        # 高级项先建、按钮后建，pack 顺序才是「常用 → 按钮 → 高级设置」。
        make_auto_split_field(self.sai_advanced.body, self.auto_split_sai_var)
        self.sai_version_override_box = ComboboxLabelRow(self.sai_advanced.body, _('SAI version override'), values=[api.version_name for api in sai.get_sai_api_list()], index_variable=self.sai_version_override_var)
        self.image_size_row = ImageSizeRow(self.sai_advanced.body, self.image_size_limit_var)
        add_tooltips(self.image_size_row, _('Captured images will be scaled down to fit this size. Larger means sharper video but slower capture and bigger files.'))
        self.sai_canvas_preview = ImageLabelRow(self.sai_advanced.body, _('Canvas preview'))
        self.sai_recording_frame = VideoConfigFrame(sai_main, recording_vars,
                                                     advanced_master=self.sai_advanced.body, icon='record')
        # 恢复默认设置放在高级区最末：用得上但不想让人手滑点到。
        # 必须在 VideoConfigFrame 之后建，pack 顺序才是它排在视频格式选项下面。
        self.restore_defaults_text = LocalizedStringVar(_('Restore default settings'))
        stdttk.Button(self.sai_advanced.body, textvariable=self.restore_defaults_text,
                   command=self.restore_defaults).pack(anchor=ttkc.W, padx=6, pady=(10, 4))
        # 热键是「不用切回窗口」才成立的功能，藏在悬停提示里等于没有。
        # 注册失败时（被别的软件占用）这行会被撤掉，见下方 hotkey.start()。
        self.hotkey_hint_var = LocalizedStringVar(_('Ctrl+Alt+S starts / stops recording, even while SAI has focus.'))
        self.hotkey_hint = stdttk.Label(sai_main, textvariable=self.hotkey_hint_var,
                                        style='Muted.TLabel')
        self.hotkey_hint.pack(anchor=ttkc.W, padx=14, pady=(4, 0))

        export_frame = make_notebook_frame(notebook, _('Export Video'))
        export_main = ttk.Frame(export_frame)
        export_main.pack(fill=ttkc.X)
        self.export_advanced = AdvancedPanel(export_frame)

        make_frames_path_readonly(export_main, self.frames_file_var, self.open_frames_folder)
        export_fps_entry = EntryLabelRow(export_main, _('Export FPS'), textvariable=self.export_fps_var)
        export_time_limit_entry = EntryLabelRow(export_main, _('Export time limit'), textvariable=self.export_time_limit_var)
        self.export_summary_row = StatusLabelRow(export_main, _('This session'), textvariable=self.export_summary_var)
        export_preview_last_frame_first = CheckbuttonLabelRow(self.export_advanced.body, _('Preview last frame first'), variable=self.export_preview_last_frame_first_var)
        export_preview_duration = EntryLabelRow(self.export_advanced.body, _('Preview duration'), textvariable=self.export_preview_duration)
        export_user_recording = CheckbuttonLabelRow(self.export_advanced.body, _('Use recording video type'), variable=self.export_use_recording_var)
        self.export_config_frame = VideoConfigFrame(export_main, export_vars,
                                                    advanced_master=self.export_advanced.body, icon='export')
        self.export_progress = ProgressRow(export_main, font=fixed_font)
        self.export_result_row = StatusLabelRow(export_main, _('Saved to'), textvariable=self.export_result_var)
        # 导出完成才出现的一朵小花 —— 全屏唯一允许的实心图形
        # 导出完成才出现的一朵小花 —— 全屏唯一允许的实心图形
        self.export_flower = ornaments.Ornament(self.export_result_row, kind='flower',
                                                size=16, token='danger')
        self.export_open_button = stdttk.Button(
            self.export_result_row, textvariable=LocalizedStringVar(_('Open')), width=8,
            command=self.open_export_folder, state=ttkc.DISABLED)
        self.export_open_button._icon_name = 'open'
        self._apply_export_open_icon()
        self.export_open_button.pack(side=ttkc.LEFT, padx=(6, 0))

        self.status_area = status_area = StatusArea(self, font=fixed_font)
        logger = AsyncWidgetLogger(status_area)
        logging.getLogger().addHandler(logger)

        self.stop_recording_label = LocalizedStringVar(_('Stop Recording'))
        self.stop_exporting_label = LocalizedStringVar(_('Stop Exporting'))

        add_tooltips(self.sai_version_override_box, _('If the running SAI version cannot be detected, the selected override will be used.'))
        add_tooltips(self.sai_canvas_box, _('Select which open SAI canvas to record from.'))
        add_tooltips(export_time_limit_entry, _('Maximum time the exported video should be. Enter 0 or leave blank for no time limit.'))
        add_tooltips(export_fps_entry, _('Set the FPS of the exported video. 30 is a common default.'))
        add_tooltips(export_preview_last_frame_first, _('Display the last frame at the beginning of the exported video, like a preview of the finished timelapse.'))
        add_tooltips(export_preview_duration, _('The duration of the preview frame in seconds. Enter 0 or leave blank to display for only a single frame. This adds on to the final export duration.'))
        add_tooltips(export_user_recording, _('Use the same video options as the recording tabs and ignore the below settings.'))

        # 页脚上方一条 1px 线，把「设置区」和「操作区」分开
        flat_tk(tk.Frame, self, 'border', height=1).pack(fill=ttkc.X)
        meta_config = ttk.Frame(self)
        meta_config.pack(fill=ttkc.X, padx=12, pady=(6, 8))
        theme_label = LocalizedStringVar(_('Theme:'))
        stdttk.Label(meta_config, textvariable=theme_label, style='Muted.TLabel').pack(side=ttkc.LEFT)
        meta_config_theme_box = stdttk.Combobox(meta_config, state=ttkc.READONLY, width=9)
        meta_config_theme_box.pack(side=ttkc.LEFT, padx=(6, 0))
        # 下拉里显示中文名，存进设置的仍是主题 id。
        # 名字从 theme.THEME_LABELS 取，别在这里再抄一份 —— 改标签时会漏。
        theme_lookup = {label: key for key, label in theme.THEME_LABELS.items()}
        theme_lookup_rev = {v: k for k, v in theme_lookup.items()}
        theme_box_var = ttk.StringVar()
        meta_config_theme_box.config(values=list(theme_lookup.keys()), textvariable=theme_box_var)

        meta_config_lang_box = stdttk.Combobox(meta_config, state=ttkc.READONLY, width=10)
        meta_config_lang_box.pack(side=ttkc.RIGHT)
        stdttk.Label(meta_config, textvariable=LocalizedStringVar(_('Language')), style='Muted.TLabel').pack(side=ttkc.RIGHT, padx=(0, 6))
        languages_lookup = {
            '简体中文': 'zh_CN',
            'English': 'en',
            '日本語': 'ja',
        }
        languages_lookup_rev = { v:k for k,v in languages_lookup.items() }
        lang_box_var = ttk.StringVar()
        info_lang_printed = set()
        def on_lang_change(*_args):
            lang_code = languages_lookup.get(lang_box_var.get())
            set_locale(lang_code)
            meta_config_lang_var.set(lang_code)
            for obj in translatables:
                obj.update_translation()
            auto_size_label_rows(self)
            if lang_code not in info_lang_printed:
                info_lang_printed.add(lang_code)
                self.print_init_info()
        lang_box_var.trace_add('write', on_lang_change)
        meta_config_lang_box.config(values=list(languages_lookup.keys()), textvariable=lang_box_var)
        meta_config_lang_box.set(languages_lookup_rev.get(meta_config_lang_var.get()))

        #########################################################
        # Init and callbacks
        self.restore_window_geometry()

        def current_tab_frame():
            # 用 nametowidget(select()) 而不是 children[index]：
            # 两者顺序不一定一致，取错就会拿另一个标签页的高度去撑当前页。
            return notebook.nametowidget(notebook.select())

        def refit_notebook():
            req = current_tab_frame().winfo_reqheight()
            if req > 1:
                notebook.config(height=req)
            return req

        def on_notebook_tab_changed(*_args):
            index = notebook.index(notebook.select())
            self.selected_tab_thread_safe = index
            self.selected_tab_var.set(index)
            # winfo_reqheight() is 1 while the frame has not been laid out yet.
            # Applying that value would collapse the notebook and then keep the
            # "fix size" loop below running forever, so ignore it.
            refit_notebook()
            # 切到导出页时重扫一次，让「本次录制」那行是当前真实数据
            if index == 1:
                self.schedule_export_summary()
        notebook.bind('<<NotebookTabChanged>>', on_notebook_tab_changed)

        def settle_notebook(attempt=0):
            """启动后补一次定高。

            打包版冷启动时第一帧还没布局完，`<<NotebookTabChanged>>` 可能算出偏大的
            req，界面就在「高级设置」和「状态」之间留一大块空白。等布局稳定再算一次。
            """
            if not self.winfo_exists():
                return
            req = refit_notebook()
            if req <= 1 and attempt < 25:
                self.after(40, lambda: settle_notebook(attempt + 1))
        self.after(400, settle_notebook)

        # 展开高级设置会把窗口撑高。记下撑高之前的高度，收起时才能回到原位；
        # None 表示「当前不是被展开撑高的状态」。
        self._fit_baseline = None

        def on_advanced_toggle():
            """展开/收起高级设置后重算 notebook 高度，并把窗口高度跟着收放。"""
            def apply():
                if not self.winfo_exists():
                    return
                self.update_idletasks()
                on_notebook_tab_changed()   # 高级区增减的行会改变当前页的自然高度
                self.update_idletasks()
                fit_window(shrink=True)
            self.after(10, apply)
            # 第一帧的 reqheight 有时还没算完（打包版冷启动尤其明显），补一枪
            self.after(120, apply)

        def fit_window(min_log_height=160, shrink=False):
            """窗口装不下时增高；shrink=True 时允许回落。

            以前只有增高分支：展开把窗口撑长后，收起时内容明明变短了，
            窗口却停在原高度 —— 状态区是 expand=True 的，多出来的空间全被它吃掉，
            看起来就是「页面被拉长了收不回去」。现在记一个基线，收起时回落到
            max(当前需要, 撑高前的高度)，既不会比撑高前更矮，也不会留一大块空白。
            """
            if not self.winfo_exists():
                return
            if self.state() == 'zoomed':
                # 最大化时 geometry() 改不动大小，算了会白清掉基线
                return
            self.update_idletasks()
            need = self.winfo_reqheight() + min_log_height
            max_h = self.winfo_screenheight() - 80
            target = min(need, max_h)
            current = self.winfo_height()
            if target > current:
                if self._fit_baseline is None:
                    self._fit_baseline = current
                self.geometry(f'{self.winfo_width()}x{target}')
            elif shrink and self._fit_baseline is not None and target < current:
                self.geometry(f'{self.winfo_width()}x{max(target, self._fit_baseline)}')
                self._fit_baseline = None
        self.sai_advanced.on_toggle = on_advanced_toggle
        self.export_advanced.on_toggle = on_advanced_toggle
        # 旧版本可能存了已被删除的标签页索引（PSD=1 / Screen=2），这里夹一下防止越界。
        tab_index = min(self.selected_tab_var.get(), len(notebook.tabs()) - 1)
        notebook.select(notebook.tabs()[tab_index])
        def hack_fix_size(attempt=0):
            if notebook.winfo_height() <= 1:
                on_notebook_tab_changed()
                if attempt < 100:
                    self.after(20, lambda: hack_fix_size(attempt + 1))
                else:
                    # Height never resolved; hand sizing back to the widget
                    # instead of looping forever.
                    notebook.config(height=0)
            else:
                fit_window()
        self.after(20, hack_fix_size)

        def on_theme_change(*_args):
            name = theme_lookup.get(theme_box_var.get())
            if not name:
                return
            meta_config_theme_var.set(name)
            self.theme_use(name)
            # ttkbootstrap 每次 theme_use 都会重建样式表，我们的覆盖必须重贴一遍
            theme.apply(self.style, name)
            # 换主题会换字体度量，标签列宽不重算就会错位（只在切语言时算过）
            self.after(30, self.relayout_after_theme)
        theme_box_var.trace_add('write', on_theme_change)
        theme_box_var.set(theme_lookup_rev.get(theme.migrate_theme_name(meta_config_theme_var.get()),
                                               theme.THEME_LABELS[theme.DEFAULT_THEME]))

        self.sai_proc = None
        self.last_pid = None
        self.current_pid = None
        self._sai_retry_after = 0.0
        self._sai_detect_failures = 0
        self._pid_scan_interval = 0.25

        self.sai_version_override_var.trace_add('write', self.on_version_override_changed)
        self.sai_canvas_var.trace_add('write', self.on_canvas_selected)
        self.bind('<FocusIn>', self.on_canvas_selected)
        self.sai_recording_frame.set_button_callback(self.recording_wrapper(True, self.record_sai))
        self.export_config_frame.set_button_callback(self.recording_wrapper(False, self.export))
        add_tooltips(self.sai_recording_frame.button.button,
                     _('Tip: Ctrl+Alt+S starts / stops recording globally, even while SAI has focus.'))
        # 这三个值都会改变成片结果，任何一个变动都要重算「本次录制」预估
        for var in (self.frames_file_var, self.export_fps_var, self.export_time_limit_var):
            var.trace_add('write', self.schedule_export_summary)
        self.schedule_export_summary()

        #########################################################
        # Background tasks
        self.pid_scan_event = threading.Event()
        # PID scan is slow, so run it on another thread.
        self.pid_scan_task = asyncio.create_task(asyncio.to_thread(self.pid_scan_thread))
        self.background_task = asyncio.create_task(self.run_background_task())
        # Nobody awaits these tasks, so a crash inside them would otherwise be
        # swallowed and the UI would silently look "stuck".
        for task in (self.pid_scan_task, self.background_task):
            task.add_done_callback(self._log_task_failure)
        self.operation_thread_running = False
        self.operation_task: asyncio.Task | None = None
        self.canvas_preview_task: asyncio.Task | None = None

        # 全局热键：画画时窗口被 SAI 挡住，不用 Alt+Tab 切回来也能停录制
        self.loop = asyncio.get_running_loop()
        self.hotkey = HotkeyListener(lambda: self.loop.call_soon_threadsafe(self.on_hotkey))
        self.hotkey.start()

        async def apply_hotkey_state():
            # 等线程报出注册结果（最多 2 秒），再决定是否保留界面上的提示。
            # 直接读 self.hotkey.registered 是不可靠的：start() 返回时线程还没跑。
            await asyncio.to_thread(self.hotkey.ready.wait, 2.0)
            if self.hotkey.registered:
                logging.info(tr(_('Global hotkey: {} to start / stop recording')).format('Ctrl+Alt+S'))
            else:
                # 热键被占用：别在界面上写着按了也没用的键
                self.hotkey_hint.pack_forget()
        self._hotkey_ready_task = asyncio.create_task(apply_hotkey_state())

    @staticmethod
    def _log_task_failure(task):
        if task.cancelled():
            return
        ex = task.exception()
        if ex is not None:
            logging.error('Background task crashed', exc_info=ex)

    def restore_window_geometry(self):
        """恢复上次的窗口位置和大小。

        以前只存 ``win_size``（宽高），最大化时存下来的是一对很大的数字，
        下次启动就变成一个「比屏幕还大的普通窗口」而不是最大化。
        """
        fallback = '{}x{}'.format(*self.win_size_var.get())
        geometry = self.settings.get_var('win_geometry', '')
        saved = geometry.get().strip() if geometry else ''
        if not saved:
            self.geometry(fallback)
            return
        try:
            size, x, y = saved.replace('+', ' +').replace('-', ' -').split()
            width, height = (int(v) for v in size.split('x'))
            x, y = int(x), int(y)
            # 换了显示器/拔了外接屏时，旧坐标可能落在屏幕外
            screen_w = self.winfo_screenwidth()
            screen_h = self.winfo_screenheight()
            if not (-width < x < screen_w and -height < y < screen_h):
                logging.info('Saved window position is off-screen, centring instead')
                self.geometry(f'{width}x{height}')
            else:
                self.geometry(f'{width}x{height}+{x}+{y}')
        except (ValueError, TypeError):
            logging.debug('Malformed saved geometry: %r', saved, exc_info=True)
            self.geometry(fallback)

        win_state = self.settings.get_var('win_state', 'normal')
        if win_state.get() == 'zoomed':
            # 等窗口真正映射出来再最大化，否则某些 ttk 主题会算错布局
            self.after(60, lambda: self.state('zoomed'))

    def save_window_geometry(self):
        # 必须走 var：Settings 的 trace 才会把值同步进 data。直接写 data 会让
        # var 变成陈旧值，而 restore_window_geometry 读的是 var。
        state_var = self.settings.get_var('win_state', 'normal')
        if self.state() == 'zoomed':
            state_var.set('zoomed')
            # 最大化时不覆盖 win_geometry，这样还原后还是最大化前的大小
        else:
            state_var.set('normal')
            self.settings.get_var('win_geometry', '').set(self.geometry())
        self.win_size_var.set((self.winfo_width(), self.winfo_height()))

    def print_init_info(self):
        session_type_key = 'XDG_SESSION_TYPE'
        session_type = os.environ.get(session_type_key, '')
        if session_type.lower() == 'wayland':
            logging.warning(tr(_('Possible Wayland session is running because the following environment variable was set:')))
            logging.warning(f'  {session_type_key}={session_type}')
            logging.warning(tr(_('Wayland sessions are currently not supported by dependencies mss and pywinctl.')))
            logging.warning(tr(_('To work around this, you can run this tool and your desired art program inside of Xwayland, see README.md.')))

        # 以前这里把 7 个版本名逐条 INFO 出来，启动就刷满半个日志区，
        # 真正要看的「SAI 未运行 / 已识别」被顶出视野。清单降到 DEBUG，INFO 只留一行。
        logging.info(tr(_('Supported SAI versions: {}')).format(len(sai.sai_api_lookup)))
        for api in sai.sai_api_lookup.values():
            logging.debug(f'  {api.version_name}')

    def cleanup(self):
        self.save_window_geometry()
        self.settings.save()
        self.hotkey.stop()
        self.pid_scan_event.set()
        self.operation_thread_running = False
        for task in [self.background_task, self.operation_task, self.canvas_preview_task]:
            if task is not None and not task.done():
                task.cancel()
                try:
                    ex = task.exception()
                    logging.exception(ex)
                except:
                    pass

    def on_hotkey(self):
        """Ctrl+Alt+S：正在录就停，没在录就开始 —— 不用切回窗口。"""
        if not self.winfo_exists():
            return
        # run_operation 在已有操作时会走「取消」分支，所以一次调用就是开关
        self.run_operation(True, self.record_sai)

    def on_version_override_changed(self, *_args):
        self.clear_sai_proc()
        self.set_sai_proc()

    def on_canvas_selected(self, *_args):
        self.update_canvas_preview()

    def schedule_export_summary(self, *_args):
        """帧目录 / 帧率 / 时长上限变动后延迟重扫，避免每敲一个字就去扫盘。"""
        if not self.winfo_exists():
            return
        if self._summary_after is not None:
            self.after_cancel(self._summary_after)
        self._summary_after = self.after(300, self.refresh_export_summary)

    def refresh_export_summary(self):
        """重算导出页的「本次录制」那一行：多少帧、素材多长、成片多长。"""
        self._summary_after = None
        if self._summary_task is not None and not self._summary_task.done():
            self._summary_task.cancel()
        async def task():
            try:
                frames_path = timelapse.resolve_frames_dir(self.frames_file_var.get())
                _files, frame_count, _size = await asyncio.to_thread(timelapse.scan_frames, frames_path)
            except Exception as ex:
                logging.debug('Frame scan failed', exc_info=ex)
                frame_count = 0
            self.export_summary_var.set(self.format_summary(frame_count))
        self._summary_task = asyncio.create_task(task())
        self._summary_task.add_done_callback(self._log_task_failure)

    def format_summary(self, frame_count:int) -> str:
        if frame_count <= 0:
            return tr(_('No frames recorded yet'))
        fps = clamp_number(self.export_fps_var.get(), 1, 120, 30)
        limit = clamp_number(self.export_time_limit_var.get(), 0, 86400, 0)
        source_seconds = frame_count / fps
        output_seconds = source_seconds if limit <= 0 else min(source_seconds, limit)
        text = tr(_('{} frames · {} source → {} video')).format(
            frame_count, format_duration(source_seconds), format_duration(output_seconds))
        if limit > 0 and source_seconds > limit + 0.5:
            text += ' ' + tr(_('(sampled)'))
        return text

    def restore_defaults(self):
        """把设置恢复成默认值。帧目录/主题/语言/窗口位置保留。"""
        if not messagebox.askokcancel(
                title=tr(_('Restore default settings')),
                message=tr(_('All settings except the frames folder, theme, language and window position will be reset. Continue?')),
                parent=self):
            return
        keep = {'frames_file', 'theme', 'meta_config_lang', 'win_geometry', 'win_state'}
        restored = self.settings.reset(keep=keep)
        logging.info(tr(_('Settings restored to defaults ({} items)')).format(len(restored)))
        messagebox.showinfo(title=tr(_('Restore default settings')),
                            message=tr(_('Settings restored to defaults.')),
                            parent=self)

    def make_recording_badge(self, master):
        """录制中的红点徽标。默认不显示 —— 空闲时界面不该有任何红色。"""
        badge = ttk.Frame(master)
        dot = flat_tk(tk.Canvas, badge, 'window', width=12, height=12,
                      highlightthickness=0, borderwidth=0)
        dot.create_oval(3, 3, 9, 9, fill=theme.color('danger_dot'), outline='', tags='dot')
        dot.pack(side=ttkc.LEFT)
        stdttk.Label(badge, textvariable=LocalizedStringVar(_('Recording')),
                     style='Muted.TLabel').pack(side=ttkc.LEFT, padx=(4, 0))
        self.recording_dot = dot
        self._recording_badge_frame = badge
        return badge

    def set_recording_badge(self, visible: bool):
        badge = self.recording_badge
        if badge is None or not self.winfo_exists():
            return
        if visible:
            # before= 只认已经 pack 过的兄弟控件，否则 TclError。
            # 取第一个真正可见的兄弟，没有就按默认顺序追加。
            anchor = next((w for w in badge.master.winfo_children()
                           if w is not badge and w.winfo_manager() == 'pack'), None)
            opts = dict(fill=ttkc.X, pady=(0, 4), padx=12)
            if anchor is not None:
                badge.pack(before=anchor, **opts)
            else:
                badge.pack(**opts)
        else:
            badge.pack_forget()

    def style_popdowns(self):
        """Combobox 的下拉列表不归样式表管，只能靠 option database 上色。

        popdown 是第一次展开时才创建的 Tcl listbox，所以在这里设置对
        已经建好的 Combobox 同样有效。
        """
        for pattern, token in (
            ('*TCombobox*Listbox.background', 'input'),
            ('*TCombobox*Listbox.foreground', 'text'),
            ('*TCombobox*Listbox.selectBackground', 'selection'),
            ('*TCombobox*Listbox.selectForeground', 'text'),
            ('*TCombobox*Listbox.borderWidth', None),
        ):
            try:
                value = '0' if token is None else theme.color(token)
                self.option_add(pattern, value)
            except Exception:
                logging.debug('Could not set %s', pattern, exc_info=True)

    def theme_titlebar(self):
        """把系统标题栏刷成和界面一致的颜色（切主题后要重刷）。"""
        try:
            if not self.winfo_exists():
                return
            mode = apply_titlebar_theme(self, theme.tokens(), dark=theme.is_dark())
            logging.debug('Title bar themed via %s', mode)
        except Exception:
            logging.debug('Title bar theming failed', exc_info=True)

    def apply_window_background(self):
        """根窗口自己的背景不归样式表管，不设就是系统面板色 #F0F0F0。

        所有内容区都有 padx/pady，露出来的那圈窗口底色就是这里。
        """
        try:
            self.configure(background=theme.color('window'))
        except Exception:
            logging.debug('Could not paint the root window', exc_info=True)

    def relayout_after_theme(self):
        """换主题后的收尾：重算标签列宽 + 重刷装饰。

        两件事分开兜异常 —— 列宽算失败不该连带装饰一起不换色。
        """
        self.apply_window_background()
        try:
            auto_size_label_rows(self)
        except Exception:
            logging.debug('Could not re-measure label rows', exc_info=True)
        self.theme_titlebar()
        self.style_popdowns()
        self.refresh_ornaments()

    def refresh_ornaments(self):
        """切主题后按新令牌重画装饰（颜色是画上去的，不会跟着样式自动变）。

        每块单独兜异常：一处失败不该让后面的红点、描边、空状态一起掉色。
        """
        steps = (
            ('ornaments', self._redraw_ornaments),
            ('button icons', self._recolor_icons),
            ('flat tk widgets', repaint_flat_tk),
            ('advanced headers', self._recolor_advanced),
            ('log tags', self._recolor_log),
            ('recording dot', self._recolor_dot),
            ('canvas preview', self._recolor_preview),
        )
        for name, step in steps:
            try:
                step()
            except Exception:
                logging.debug('Could not refresh %s', name, exc_info=True)

    def _redraw_ornaments(self):
        for widget in get_widgets_by_type(self, ornaments.Ornament):
            widget.redraw(color=None)

    def _recolor_advanced(self):
        """折叠面板的箭头/标题色是直接写进控件的，样式表换主题时不会跟着变。"""
        for panel in get_widgets_by_type(self, AdvancedPanel):
            panel.refresh()

    def _apply_export_open_icon(self):
        photo = icons.make('open', 'text_muted')
        if photo is not None:
            self.export_open_button.config(image=photo, compound=ttkc.LEFT)

    def _recolor_icons(self):
        """切主题后按新令牌重建图标 —— 图标是位图，不会自己变色。"""
        for row in get_widgets_by_type(self, ButtonRow):
            row.set_icon(row._icon_name)
        for row in get_widgets_by_type(self, EntryLabelRow):
            row._apply_button_icon()
        self._apply_export_open_icon()

    def _recolor_log(self):
        area = getattr(self, 'status_area', None)
        if area is None:
            return
        area.text.config(background=theme.color('sunken'),
                              foreground=theme.color('text_muted'),
                              insertbackground=theme.color('text'),
                              selectbackground=theme.color('selection'))
        area._apply_tags()

    def _recolor_dot(self):
        if self.recording_dot is not None:
            self.recording_dot.config(background=theme.color('window'))
            self.recording_dot.itemconfig('dot', fill=theme.color('danger_dot'))

    def _recolor_preview(self):
        self.sai_canvas_preview.draw_empty()

    def show_error(self, ex):
        """操作失败时弹窗 —— 只写日志的话，用户只会看到按钮回弹，以为没事发生。"""
        if not self.winfo_exists():
            return
        message = str(ex).strip() or type(ex).__name__
        try:
            messagebox.showerror(title=tr(_('Error')), message=message, parent=self)
        except Exception:
            logging.exception('Could not show error dialog')

    def update_canvas_preview(self, load_wait=False):
        if self.sai_proc is None:
            self.sai_canvas_preview.remove_image()
            return
        if self.canvas_preview_task is not None:
            self.canvas_preview_task.cancel()
        async def task():
            canvases = self.sai_proc.api.get_canvas_list()
            if not canvases:
                self.sai_canvas_preview.remove_image()
                return
            target_canvas = canvases[self.sai_canvas_box.get_index()]
            def canvas_get():
                if load_wait:
                    time.sleep(1.0)
                map_level = self.sai_proc.api.get_map_level_for_size(target_canvas, 100)
                image = self.sai_proc.api.get_canvas_image(target_canvas, map_level)
                image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                image = Image.fromarray(image)
                image.thumbnail((100, 100))
                return image
            image = await asyncio.to_thread(canvas_get)
            self.sai_canvas_preview.set_image(image)
        self.canvas_preview_task = asyncio.create_task(task())

    def clear_sai_proc(self):
        if self.sai_proc is not None:
            self.sai_proc.close()
            self.sai_proc = None
            self.last_pid = None

    def refresh_sai_canvses(self):
        canvases = [f'{canvas.get_name()} ({canvas.get_short_path()})' for canvas in self.sai_proc.api.get_canvas_list()]
        previous_canvases = self.sai_canvas_box.get_values()
        self.sai_canvas_box.set_values(canvases)
        if canvases != previous_canvases:
            self.update_canvas_preview(load_wait=True)

    def pid_scan_thread(self):
        while not self.pid_scan_event.wait(self._pid_scan_interval):
            if self.sai_proc is None and self.operation_task is None and self.selected_tab_thread_safe == 0:
                self.current_pid = sai.find_running_sai_pid()
                # 扫一次要枚举全部进程（约 18ms）。空闲时每 250ms 扫一遍等于
                # 白白烧掉 7% CPU，所以没找到 SAI 就退避到 2 秒。
                self._pid_scan_interval = 0.25 if self.current_pid is not None else 2.0
            else:
                # 正在录制/不在录制页：保持快轮询，切回来时能立刻接上
                self._pid_scan_interval = 0.25

    def set_sai_proc(self):
        if self.sai_proc is not None and self.sai_proc.is_running():
            self.refresh_sai_canvses()
        else:
            self.clear_sai_proc()
            sai_pid = self.current_pid
            if sai_pid is None:
                self.sai_version_status_var.set(_('SAI is not running'))
            elif sai_pid != self.last_pid and time.time() >= self._sai_retry_after:
                try:
                    self.last_pid = sai_pid
                    api = sai.get_sai_api_from_pid(sai_pid)
                    # Found version
                    if api is not None:
                        self.sai_proc = sai.SAI()
                        self.sai_version_status_var.set(self.sai_proc.api.version_name)
                    # Override version
                    else:
                        version_index = self.sai_version_override_var.get()
                        api = sai.get_sai_api_list()[version_index]
                        self.sai_proc = sai.SAI(api)
                        self.sai_version_status_var.set(f'{self.sai_proc.api.version_name}' + tr(_('(Override)')))
                    self.refresh_sai_canvses()
                except sai.NoSaiProcessDetected:
                    # The process exists but its memory is not readable yet
                    # (still starting up, or a permission issue). Retry later
                    # instead of giving up permanently -- last_pid was set
                    # before the call, so it must be cleared here.
                    self.last_pid = None
                    self._sai_detect_failures += 1
                    if self._sai_detect_failures <= 3:
                        logging.warning('SAI process found but could not be read',
                                        exc_info=True)
                    self._sai_retry_after = time.time() + 2.0

    async def run_background_task(self):
        while True:
            if self.operation_task is None and self.selected_tab_var.get() == 0:
                try:
                    self.set_sai_proc()
                except Exception as ex:
                    logging.exception(ex)
            await asyncio.sleep(0.25)

    def run_operation(self, recording_mode, task, *args, **kwargs):
        if self.operation_task is not None:
            self.operation_task.cancel()
            self.operation_task = None
            self.operation_thread_running = False
            return
        if recording_mode:
            start_log = _('Recording started')
            stop_log = _('Recording stopped')
            stop_text = self.stop_recording_label
            own_var = self.recording_button_text_var
            own_row = self.sai_recording_frame.button
            own_button = own_row.button
            other_button = self.export_config_frame.button.button
        else:
            start_log = _('Exporting started')
            stop_log = _('Exporting stopped')
            stop_text = self.stop_exporting_label
            own_var = self.export_button_text_var
            own_row = self.export_config_frame.button
            own_button = own_row.button
            other_button = self.sai_recording_frame.button.button
        async def subtask():
            logging.info(tr(start_log))
            # 只让「当前操作」的按钮变成停止，另一个置灰。
            # 以前两个按钮一起变「停止录制」，切到导出页会看到一个莫名其妙的按钮。
            own_var.push(stop_text)
            other_button.config(state=ttkc.DISABLED)
            # 录制中：按钮变红描边（危险动作语义），并亮出红点
            own_button.config(style='Danger.TButton' if recording_mode else 'Accent.TButton')
            # 录制中换成停止方块，一眼能看出「再点一下就停」
            own_row.set_icon('stop' if recording_mode else own_row._icon_name)
            if recording_mode:
                # 立刻把标题切到「录制中 0 帧」：标题原本只在写入第一帧时更新，
                # 用户开始录制后如果迟迟没动笔，看起来就像没在录。
                self._recording_started_at = time.time()
                self.update_recording_title(0)
                self.set_recording_badge(True)
            try:
                await task(*args, **kwargs)
            except asyncio.CancelledError:
                pass
            except Exception as ex:
                logging.exception(ex)
                # 失败必须让用户看见，否则就是「点了没反应」
                self.show_error(ex)
            finally:
                logging.info(tr(stop_log))
                own_var.pop()
                other_button.config(state=ttkc.NORMAL)
                own_button.config(style='Accent.TButton')
                own_row.set_icon('record' if recording_mode else own_row._icon_name)
                if recording_mode:
                    self.set_recording_badge(False)
                if self.winfo_exists():
                    self.title(self.base_title)
                self.operation_task = None
                if recording_mode:
                    # 刚录完，导出页的「本次录制」需要立刻反映新数据
                    self.schedule_export_summary()
        self.operation_task = asyncio.create_task(subtask())

    def recording_wrapper(self, recording_mode, task, *args, **kwargs):
        return lambda: self.run_operation(recording_mode, task, *args, **kwargs)

    async def record_sai(self):
        if self.sai_proc is None:
            logging.info(tr(_('No valid SAI process found')))
            return
        # 没设保存位置就开录的话，make_session_dir('') 会把 Path('.') 当根目录，
        # 帧直接写进程序当前目录 —— 发布版可能装在 Program Files 里，直接失败。
        if not self.frames_file_var.get().strip():
            logging.warning(tr(_('Choose a save folder first — nothing is recorded without one.')))
            return
        with sai.SAI(type(self.sai_proc.api)) as sai_proc:
            canvases = sai_proc.api.get_canvas_list()
            if not canvases:
                logging.info(tr(_('There are no canvases open')))
                return
            canvas = canvases[self.sai_canvas_box.get_index()]
            # 每次录制独立成目录，避免与上一次的帧拼在一起
            frames_path = timelapse.make_session_dir(self.frames_file_var.get())
            # 记下画布名，导出时拿它给成片命名（时间戳目录名认不出画的是哪张）
            meta = {'started': datetime.now().isoformat(timespec='seconds')}
            try:
                meta['canvas'] = canvas.get_name()
                meta['canvas_path'] = canvas.get_short_path()
            except Exception:
                logging.debug('Could not read canvas name', exc_info=True)
            meta['name'] = timelapse.safe_filename(meta.get('canvas', ''))
            timelapse.write_session_meta(frames_path, meta)
            logging.info(tr(_('Recording session:')) + f' {frames_path}')
            container, codec = self.sai_recording_frame.get_format()
            image_size_limit = int(clamp_number(self.image_size_limit_var.get(), 100, 16384, 1920))
            auto_split = int(clamp_number(self.auto_split_sai_var.get(), 0, 100000, 0))
            reason = await timelapse.sai_capture(
                sai_proc, canvas, image_size_limit, frames_path, container, codec, auto_split,
                on_progress=self.update_recording_title,
            )
            # 录制会因为画布被关掉而提前结束，这件事必须在日志里说清楚
            if reason == 'canvas_lost':
                logging.warning(tr(_('Recording stopped: the SAI canvas was closed')))
            elif reason == 'no_window':
                logging.warning(tr(_('Recording stopped: could not find the SAI window')))

    def update_recording_title(self, frames:int):
        """把「已录多少帧 / 多久」写到标题栏 —— 窗口被 SAI 挡住时，这是唯一能看到的状态。"""
        elapsed = time.time() - self._recording_started_at
        if not self.winfo_exists():
            return
        self.title(f'{self.base_title} — ' + tr(_('Recording {} frames · {}')).format(frames, format_duration(elapsed)))

    def show_export_flower(self):
        """在「已导出」行尾点亮一朵花。一行只留最新一次结果，不累积。

        放在行尾而不是行首：行首会挤动标签列，把「已导出」推到其它行的
        标签列之外；行尾紧挨「打开」按钮，读作「这次成了」。
        """
        flower = self.export_flower
        if flower is None or not self.winfo_exists():
            return
        if flower.winfo_manager() == '':
            flower.pack(side=ttkc.LEFT, padx=(8, 0), before=self.export_open_button)

    def open_frames_folder(self):
        self.open_folder(timelapse.resolve_frames_dir(self.frames_file_var.get()))

    def open_export_folder(self):
        path = self.export_result_var.get()
        if path:
            self.open_folder(Path(path).parent)

    def open_folder(self, path):
        try:
            path = Path(path)
            if not path.exists():
                self.show_error(tr(_('Folder does not exist: {}')).format(path))
                return
            os.startfile(str(path))
        except Exception as ex:
            logging.exception('Could not open folder')
            self.show_error(ex)

    async def export(self):
        if not self.frames_file_var.get().strip():
            logging.warning(tr(_('Choose a save folder first — nothing is recorded without one.')))
            return
        # 有会话子目录就取最新一次录制，平铺的旧数据原样使用
        frames_path = timelapse.resolve_frames_dir(self.frames_file_var.get())
        logging.info(tr(_('Using recording session:')) + f' {frames_path}')
        export_time_limit = clamp_number(self.export_time_limit_var.get(), 0, 86400, 0)
        export_preview_last_frame = self.export_preview_last_frame_first_var.get()
        export_preview_duration = clamp_number(self.export_preview_duration.get(), 0, 60, 0)
        export_fps = clamp_number(self.export_fps_var.get(), 1, 120, 30)
        if self.export_use_recording_var.get():
            container, codec = self.sai_recording_frame.get_format()
        else:
            container, codec = self.export_config_frame.get_format()
        # 只有一个目录：成片落在会话子目录旁边
        output_path = ''
        # 录制时记下的画布名 → 成片名；老会话没有记录就退回时间戳目录名
        output_name = timelapse.read_session_meta(frames_path).get('name') or ''
        def progress_kill_check(iterable, total, unit):
            for it in self.export_progress.iterate(iterable, total, unit):
                yield it
                if not self.operation_thread_running:
                    logging.info(tr(_('Export cancelled')))
                    raise ExportCancelled()
        self.operation_thread_running = True
        # Export can have long CPU-bound sections, so run it on another thread.
        try:
            saved = await asyncio.to_thread(timelapse.export, progress_kill_check, export_time_limit, export_preview_last_frame, export_preview_duration, export_fps, frames_path, container, codec, output_path, output_name)
        except ExportCancelled:
            return
        # 告诉用户成片到底存哪了，并允许直接打开所在文件夹
        if saved:
            self.export_result_var.set(str(saved))
            self.export_open_button.config(state=ttkc.NORMAL)
            self.show_export_flower()

    def report_callback_exception(self, exc_type, exc_value, exc_tb):
        logging.exception(''.join(traceback.format_exception(exc_type, exc_value, exc_tb)))

class ExportCancelled(Exception):
    pass

# ---------------------------------------------------------------- 标题栏配色
# 深色应用顶着一条白色系统标题栏，是全界面最扎眼的一处「套壳感」。
# Win11 能直接指定标题栏/标题文字/边框颜色（DWMWA_CAPTION_COLOR 等），
# Win10 2004+ 只能整体切成深色模式（DWMWA_USE_IMMERSIVE_DARK_MODE）。
# 两套都试，都不支持就静默放弃 —— 标题栏难看总好过起不来。
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19


def _colorref(hex_color):
    """#RRGGBB -> Windows 的 COLORREF（0x00BBGGRR）。"""
    try:
        value = hex_color.lstrip('#')
        r, g, b = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
        return r | (g << 8) | (b << 16)
    except (ValueError, IndexError):
        return None


def apply_titlebar_theme(window, tokens, dark=True):
    """按当前令牌给系统标题栏上色。返回实际生效的方式，便于自检。"""
    if os.name != 'nt':
        return None
    try:
        user32 = ctypes.windll.user32
        dwm = ctypes.windll.dwmapi
        hwnd = user32.GetAncestor(window.winfo_id(), 2) or window.winfo_id()  # GA_ROOT
        if not hwnd:
            return None

        def set_attr(attr, value):
            buf = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(buf), ctypes.sizeof(buf)) == 0

        # Win11：逐项上色，能和主题完全对齐
        caption = _colorref(tokens['window'])
        text = _colorref(tokens['text'])
        border = _colorref(tokens['border'])
        if caption is not None and set_attr(_DWMWA_CAPTION_COLOR, caption):
            set_attr(_DWMWA_TEXT_COLOR, text)
            set_attr(_DWMWA_BORDER_COLOR, border)
            return 'caption'

        # Win10 1809~1909 用的是 19 号属性
        for attr in (_DWMWA_USE_IMMERSIVE_DARK_MODE, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
            if set_attr(attr, 1 if dark else 0):
                return 'dark-mode'
        return None
    except Exception:
        logging.debug('Could not theme the title bar', exc_info=True)
        return None

# 单实例：同一时间只允许一个窗口。双击两次会开两个实例、同时录同一个画布，
# 结果是两份帧目录，导出时还分不清哪份是最新的。
SINGLE_INSTANCE_MUTEX = f'Local\\{APP_NAME}-single-instance'
_single_instance_handle = None

def acquire_single_instance():
    """抢到命名互斥量就返回 True；已有实例在跑则返回 False。非 Windows 直接放行。"""
    global _single_instance_handle
    if os.name != 'nt':
        return True
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _single_instance_handle = handle  # 持有到进程结束，别让系统回收
        return True
    except Exception:
        logging.debug('Single instance check failed', exc_info=True)
        return True

def activate_existing_window():
    """把已经在跑的那个窗口提到前台，让第二次双击看起来「有效果」。"""
    if os.name != 'nt':
        return
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        found = []
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value.startswith(APP_NAME):
                    found.append(hwnd)
            return True

        user32.EnumWindows(proto(callback), 0)
        if found:
            user32.ShowWindow(found[0], 9)        # SW_RESTORE
            user32.SetForegroundWindow(found[0])
    except Exception:
        logging.debug('Could not activate existing window', exc_info=True)

async def async_main():
    await App().async_main_loop()

def main():
    if not acquire_single_instance():
        activate_existing_window()
        return
    asyncio.run(async_main())

if __name__ == '__main__':
    main()