"""16px 线性图标。

Tk 没有 SVG，PIL 的 ImageDraw 又没有抗锯齿 —— 所以先按 4 倍尺寸画，
再 LANCZOS 缩回 16px，边缘比 Tk 自带的 bitmap 干净得多。

图标颜色跟随文字色：调用方传令牌名，这里查表生成，切主题时缓存里自然
会多出一份新颜色的副本（数量有限，不做回收）。
"""
from __future__ import annotations

import logging

from PIL import Image, ImageDraw, ImageTk

from . import theme

SIZE = 16
SCALE = 4          # 超采样倍数
_cache = {}


def _rgba(hex_color: str):
    try:
        v = hex_color.lstrip('#')
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16), 255)
    except (ValueError, IndexError):
        return (255, 255, 255, 255)


def _w(n: int) -> int:
    """线宽：64px 画布上约 5px，缩回 16px 后是 1.25px。"""
    return max(2, round(n / 13))


def _folder(d: ImageDraw.ImageDraw, n: int, c):
    """文件夹：主体 + 左上角一个台阶，比纯矩形好认。"""
    w = _w(n)
    pts = [(6, 50), (6, 14), (24, 14), (29, 21), (58, 21), (58, 50)]
    d.line(pts + [pts[0]], fill=c, width=w, joint='curve')


def _record(d: ImageDraw.ImageDraw, n: int, c):
    """录制：实心圆。"""
    d.ellipse([12, 12, n - 12, n - 12], fill=c)


def _stop(d: ImageDraw.ImageDraw, n: int, c):
    """停止：实心圆角方块。"""
    d.rounded_rectangle([14, 14, n - 14, n - 14], radius=5, fill=c)


def _export(d: ImageDraw.ImageDraw, n: int, c):
    """导出：向下箭头落进托盘。"""
    w = _w(n)
    mid = n // 2
    d.line([(mid, 8), (mid, 40)], fill=c, width=w)
    d.line([(mid, 44), (mid - 13, 31)], fill=c, width=w)
    d.line([(mid, 44), (mid + 13, 31)], fill=c, width=w)
    d.line([(10, 54), (n - 10, 54)], fill=c, width=w)


def _open(d: ImageDraw.ImageDraw, n: int, c):
    """打开：方框 + 右上角斜箭头（外链语义）。"""
    w = _w(n)
    pts = [(40, 12), (12, 12), (12, n - 12), (52, n - 12), (52, 30)]
    d.line(pts, fill=c, width=w, joint='curve')
    d.line([(34, 30), (56, 8)], fill=c, width=w)
    d.line([(42, 8), (56, 8)], fill=c, width=w)
    d.line([(56, 8), (56, 22)], fill=c, width=w)


def _gear(d: ImageDraw.ImageDraw, n: int, c):
    """设置：三横线（滑块式），比齿轮在小尺寸下清楚。"""
    w = _w(n)
    for y, knob in ((16, 42), (32, 22), (48, 36)):
        d.line([(8, y), (n - 8, y)], fill=c, width=w)
        d.ellipse([knob - 6, y - 6, knob + 6, y + 6], fill=c)


_PAINTERS = {
    'folder': _folder,
    'record': _record,
    'stop': _stop,
    'export': _export,
    'open': _open,
    'gear': _gear,
}


def make(name: str, token: str = 'text', size: int = SIZE):
    """按令牌色生成（或取缓存）一个 PhotoImage。"""
    color = theme.color(token)
    key = (name, color, size)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    painter = _PAINTERS.get(name)
    if painter is None:
        logging.debug('Unknown icon %r', name)
        return None

    try:
        n = size * SCALE
        img = Image.new('RGBA', (n, n), (0, 0, 0, 0))
        painter(ImageDraw.Draw(img), n, _rgba(color))
        img = img.resize((size, size), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
    except Exception:
        logging.debug('Could not render icon %r', name, exc_info=True)
        return None

    _cache[key] = photo
    return photo


def clear_cache():
    _cache.clear()
