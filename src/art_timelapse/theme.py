"""界面令牌与主题注册。

颜色、字号、间距只在这里定义，`gui.py` 不写十六进制。

两套主题是**黑**和**白**（键名仍叫 sai-dark / sai-light，改了旧配置就丢）。
中性灰打底，红色只从铃铛装饰的下端和"进行中"状态里渗出来 ——
强调色使用面积不超过 5%。

这份表必须和 `webui/style.css` 里的 `--*` 变量保持一致，
否则回退到 Tk 界面时两种主题会变成两个观感。
"""
from __future__ import annotations

import logging

import ttkbootstrap as ttk
from ttkbootstrap.style import ThemeDefinition

# ---------------------------------------------------------------- 设计令牌

DARK = {
    'window': '#111316',
    'panel': '#181b1f',
    'sunken': '#0c0e10',
    'input': '#23272b',        # 输入框比窗口亮一档，才有「这是个框」的实体感
    'input_border': '#363c42',
    'border': '#262b30',
    'border_strong': '#3a4046',
    'hover': '#20252a',        # 行级悬停底色：要能看出来，又不能像选中态
    'text': '#e4e7ea',         # 正文/值：要压过标签
    'text_muted': '#8d949c',   # 标签/次要
    'text_faint': '#5c6269',
    'accent': '#6b90ab',
    'accent_hover': '#7ea3bd',
    'accent_fg': '#0d1013',      # 强调色上的文字/图标
    'danger': '#c25b54',
    'danger_text': '#d0706a',
    'danger_dot': '#cf5a52',
    'selection': '#2c3a45',
}

LIGHT = {
    'window': '#f3f3f4',
    'panel': '#fcfcfd',
    'sunken': '#fafafb',
    'input': '#ffffff',
    'input_border': '#d6d8db',
    'border': '#e3e5e8',
    'border_strong': '#c3c6ca',
    'hover': '#f1f2f4',
    'text': '#1a1c1f',
    'text_muted': '#6a7078',
    'text_faint': '#8d939a',   # 时间戳/版本号。比 #9ba1a8 深一档，否则白底上发虚
    'accent': '#4a6f8f',
    'accent_hover': '#5b82a2',
    'accent_fg': '#ffffff',
    'danger': '#b6483f',
    'danger_text': '#a03e36',
    'danger_dot': '#c04a40',
    'selection': '#d8e2ea',
}

THEMES = {'sai-dark': DARK, 'sai-light': LIGHT}
THEME_LABELS = {'sai-dark': '黑', 'sai-light': '白'}
DEFAULT_THEME = 'sai-dark'

_current = DARK


def tokens() -> dict:
    """当前生效的令牌表。"""
    return _current


def is_dark() -> bool:
    return _current is not LIGHT


def color(name: str) -> str:
    return _current.get(name, '#ff00ff')


def font(size: int = 11, weight: str = 'normal', mono: bool = False):
    """统一字体。档位只有 10（小）/ 11（正文）/ 13（标题）三档常用值。

    10px 的老界面显得密不透风 —— Windows 的默认 UI 字号是 9pt≈12px，
    正文停在 10px 是「工具软件」和「现代桌面应用」最直观的差别之一。
    """
    if mono:
        return ('Consolas', size, weight) if 'Consolas' in _families() else ('TkFixedFont', size, weight)
    family = 'Segoe UI' if 'Segoe UI' in _families() else ('Microsoft YaHei UI' if 'Microsoft YaHei UI' in _families() else 'TkDefaultFont')
    return (family, size, weight)


_families_cache = None


def _families():
    global _families_cache
    if _families_cache is None:
        import tkinter.font as tkfont
        try:
            _families_cache = set(tkfont.families())
        except Exception:
            _families_cache = set()
    return _families_cache


# ---------------------------------------------------------------- 主题注册

def _definition(name: str, palette: dict, mode: str) -> ThemeDefinition:
    return ThemeDefinition(
        name=name,
        mode=mode,
        colors={
            'primary': palette['accent'],
            'secondary': palette['border_strong'],
            'success': '#4f7a63' if mode == 'light' else '#5f8f74',
            'info': palette['accent'],
            'warning': '#a07840' if mode == 'light' else '#b99663',
            'danger': palette['danger'],
            'light': palette['panel'],
            'dark': palette['sunken'],
            'bg': palette['window'],
            'fg': palette['text'],
            'selectbg': palette['selection'],
            'selectfg': palette['text'],
            'border': palette['border'],
            'inputfg': palette['text'],
            'inputbg': palette['sunken'],
            'active': palette['panel'],
        },
    )


def register(style) -> None:
    """把 sai-dark / sai-light 注册进 ttkbootstrap，必须在 theme_use 之前调用。"""
    for name, palette in THEMES.items():
        mode = 'dark' if name == 'sai-dark' else 'light'
        try:
            style.register_theme(_definition(name, palette, mode))
        except Exception:
            logging.exception('Could not register theme %s', name)


def migrate_theme_name(name: str) -> str:
    """旧配置里的 bootstrap-dark 迁到 sai-dark，其余未知主题回落到默认。"""
    if name in THEMES:
        return name
    return 'sai-dark' if name != 'sai-light' else 'sai-light'


# ---------------------------------------------------------------- 样式应用

def apply(style, theme_name: str = DEFAULT_THEME) -> None:
    """在当前主题之上覆盖出"扁平、1px 线、圆角 2"的控件样式。

    ttkbootstrap 每次 theme_use 都会重建样式表，所以切主题后必须重新调用。
    """
    global _current
    _current = THEMES.get(theme_name, DARK)
    is_dark = theme_name != 'sai-light'

    t = _current
    body = font(11)
    small = font(10)
    title = font(13)
    mono = font(10, mono=True)

    # --- 基础容器 ---
    style.configure('TFrame', background=t['window'])
    style.configure('Panel.TFrame', background=t['panel'])
    style.configure('Card.TFrame', background=t['panel'])
    # ScrolledText 内部那层 bootstyle='highlight' 容器用它，否则退回系统浅色
    style.configure('Sunken.TFrame', background=t['sunken'])
    style.configure('TLabel', background=t['window'], foreground=t['text'], font=body)
    # 表单标签：退到次要层。之前标签是亮白、值是暗灰，主次反了
    style.configure('RowLabel.TLabel', background=t['window'],
                    foreground=t['text_muted'], font=small)
    style.configure('Muted.TLabel', background=t['window'], foreground=t['text_muted'], font=small)
    style.configure('Faint.TLabel', background=t['window'], foreground=t['text_faint'], font=small)
    style.configure('Section.TLabel', background=t['window'], foreground=t['text_muted'], font=small)
    style.configure('Title.TLabel', background=t['window'], foreground=t['text'], font=title)
    style.configure('TSeparator', background=t['border'])
    style.configure('TLabelframe', background=t['window'], bordercolor=t['border'], borderwidth=1, relief='solid')
    style.configure('TLabelframe.Label', background=t['window'], foreground=t['text_muted'], font=small)

    # --- 按钮：扁平 + 1px 边框 ---
    style.configure('TButton',
                    background=t['panel'], foreground=t['text'],
                    bordercolor=t['border'], borderwidth=1,
                    relief='flat', focusthickness=0,
                    padding=(14, 7), font=body, anchor='center')
    style.map('TButton',
              background=[('pressed', t['sunken']), ('active', t['selection']), ('disabled', t['window'])],
              foreground=[('disabled', t['text_faint'])],
              bordercolor=[('pressed', t['border_strong']), ('active', t['border_strong'])])

    # 主按钮：实心强调色，深字（唯一大面积彩色）
    style.configure('Accent.TButton',
                    background=t['accent'], foreground=t['accent_fg'],
                    bordercolor=t['accent'], borderwidth=1, relief='flat',
                    padding=(16, 7), font=font(11, 'bold'))
    style.map('Accent.TButton',
              background=[('pressed', t['accent']), ('active', t['accent_hover']), ('disabled', t['panel'])],
              foreground=[('disabled', t['text_faint'])],
              bordercolor=[('active', t['accent_hover'])])

    # 停止录制：红描边不填充
    style.configure('Danger.TButton',
                    background=t['panel'], foreground=t['danger_text'],
                    bordercolor=t['danger'], borderwidth=1, relief='flat',
                    padding=(16, 7), font=font(11, 'bold'))
    style.map('Danger.TButton',
              background=[('pressed', t['sunken']), ('active', t['sunken'])],
              foreground=[('disabled', t['text_faint'])])

    # 幽灵按钮（日志区工具条）
    style.configure('Ghost.TButton',
                    background=t['panel'], foreground=t['text_muted'],
                    bordercolor=t['border'], borderwidth=1, relief='flat',
                    padding=(10, 4), font=small)
    style.map('Ghost.TButton',
              background=[('pressed', t['sunken']), ('active', t['window'])],
              foreground=[('active', t['text'])])

    # --- 输入类：比背景亮一档，才像个能点的框 ---
    for name in ('TEntry', 'TCombobox'):
        style.configure(name,
                        fieldbackground=t['input'], background=t['input'],
                        foreground=t['text'], bordercolor=t['input_border'],
                        borderwidth=1, relief='flat',
                        padding=(9, 6), arrowcolor=t['text_muted'],
                        selectbackground=t['selection'], selectforeground=t['text'])
        style.map(name,
                  bordercolor=[('focus', t['accent'])],
                  fieldbackground=[('readonly', t['input']), ('disabled', t['window'])],
                  foreground=[('readonly', t['text']), ('disabled', t['text_faint'])])

    # 只读输入框（显示路径/摘要）：底色压回窗口色，明确「不可编辑」
    style.configure('Readonly.TEntry',
                    fieldbackground=t['window'], background=t['window'],
                    foreground=t['text_muted'], bordercolor=t['border'],
                    borderwidth=1, relief='flat', padding=(9, 6))

    # --- 标签页：选中态做成实底块，比一根蓝字清楚 ---
    style.configure('TNotebook', background=t['window'], borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure('TNotebook.Tab',
                    background=t['window'], foreground=t['text_muted'],
                    bordercolor=t['window'], borderwidth=0,
                    padding=(18, 9), font=body)
    style.map('TNotebook.Tab',
              background=[('selected', t['panel']), ('active', t['panel'])],
              foreground=[('selected', t['text']), ('active', t['text'])],
              expand=[('selected', (0, 0, 0, 0))])

    # --- 滚动条：细，无箭头 ---
    style.configure('Vertical.TScrollbar',
                    background=t['border'], troughcolor=t['sunken'],
                    bordercolor=t['sunken'], arrowcolor=t['sunken'],
                    borderwidth=0, width=10, arrowsize=0)
    style.map('Vertical.TScrollbar', background=[('active', t['border_strong'])])

    # --- 复选框 ---
    style.configure('TCheckbutton',
                    background=t['window'], foreground=t['text_muted'],
                    indicatorcolor=t['input'], font=small)
    style.map('TCheckbutton',
              background=[('active', t['window'])],
              indicatorcolor=[('selected', t['accent']), ('active', t['border_strong'])],
              foreground=[('selected', t['text'])])

    # --- 进度条：3px 细条 ---
    style.configure('TProgressbar',
                    background=t['accent'], troughcolor=t['sunken'],
                    bordercolor=t['sunken'], borderwidth=0, thickness=3)
