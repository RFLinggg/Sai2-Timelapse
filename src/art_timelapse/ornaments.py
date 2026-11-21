"""铃铛与花瓣的矢量绘制。

Tk 没有 SVG，这里把设计稿里的路径换算成 `create_line(smooth=True)` 与 `create_arc`。
尺寸只允许 12 / 16 / 26 / 36 / 48 五档 —— 不出现「不大不小」的中间值。
"""
from __future__ import annotations

import math

import ttkbootstrap as ttk
import ttkbootstrap.constants as ttkc

from . import theme as tokens

# 12 行内标记 / 16 行内花标 / 26 卡片 / 36 空状态 / 48 大图 —— 只用这几档，
# 不出现「不大不小」的中间值。
SIZES = (12, 16, 26, 36, 48)


def _bell_path(size: float):
    """24×24 视图框下的铃铛轮廓，按 size/24 缩放后返回点列。"""
    k = size / 24.0

    def p(x, y):
        return (x * k, y * k)

    # 铃身：弧顶 + 外扩裙边（闭合多边形，smooth 之后是弧线）
    body = [
        p(12, 3), p(9, 3), p(7.2, 5.4), p(7.2, 8.2),
        p(7.2, 11), p(7.2, 13), p(5.4, 16), p(18.6, 16),
        p(16.8, 13), p(16.8, 11), p(16.8, 8.2), p(16.8, 5.4),
        p(15, 3),
    ]
    return body, k


def draw_bell(canvas, size: int, color: str, x: float = 0, y: float = 0):
    """在 canvas 的 (x, y) 处画一枚线描铃铛。

    用「圆弧顶盖 + 直线裙边」而不是一条平滑样条：样条会把顶盖拉平，
    26px 以上看起来像个方盒子。
    """
    k = size / 24.0
    width = max(1, round(1.6 * k))

    def s(v):
        return v * k

    # 顶盖：以 (12, 8.2) 为心、半径 4.6 的上半圆
    canvas.create_arc(x + s(7.4), y + s(3.6), x + s(16.6), y + s(12.8),
                      start=0, extent=180, style='arc',
                      outline=color, width=width)
    # 裙边：两侧竖直下落，再外扩到底沿
    canvas.create_line(
        x + s(16.6), y + s(8.2),
        x + s(16.6), y + s(13.0),
        x + s(18.6), y + s(16.0),
        x + s(5.4), y + s(16.0),
        x + s(7.4), y + s(13.0),
        x + s(7.4), y + s(8.2),
        fill=color, width=width, capstyle='round', joinstyle='round')
    # 铃舌：底部一段小弧
    r = 2.1 * k
    canvas.create_arc(x + s(12) - r, y + s(18) - r,
                      x + s(12) + r, y + s(18) + r,
                      start=180, extent=180, style='arc',
                      outline=color, width=width)
    # 顶钮：太小的时候画上去会糊成一团，直接省掉
    if size >= 26:
        canvas.create_line(x + s(12), y + s(3.6), x + s(12), y + s(1.6),
                           fill=color, width=width, capstyle='round')
    return size


def _petal_points(size: float):
    """单片花瓣（16×20 视图框）的点列，按 size/16 缩放。"""
    k = size / 16.0

    def p(x, y):
        return (x * k, y * k)

    return [p(8, 1), p(11.5, 5.5), p(11.5, 9), p(8, 13),
            p(4.5, 9), p(4.5, 5.5)], k


def draw_petal(canvas, size: int, color: str, x: float = 0, y: float = 0, vein: bool = True):
    """竖着的单瓣，作项目符号用。"""
    pts, k = _petal_points(size)
    flat = []
    for px, py in pts:
        flat.extend((x + px, y + py))
    width = max(1, round(1.6 * k))
    canvas.create_line(*flat, smooth=True, splinesteps=10, fill=color,
                       width=width, joinstyle='round', capstyle='round')
    if vein:
        canvas.create_line(x + 8 * k, y + 4 * k, x + 8 * k, y + 10 * k,
                           fill=color, width=max(1, round(width * 0.7)), capstyle='round')


def draw_flower(canvas, size: int, color: str, cx: float = 0, cy: float = 0, filled_center: bool = True):
    """五瓣花：同一片花瓣绕圆心旋转 72° ×5。

    花瓣要「短而宽」——细长的话在 48px 会读成五角星而不是花。
    26px 以下改用实心花瓣：那个尺寸上 1px 线描的五个小圈会散成雪花/星号，
    只有填成色块才还认得出是花。
    """
    k = size / 26.0
    width = max(1, round(1.5 * k))
    solid = size < 26
    for i in range(5):
        angle = math.radians(i * 72 - 90)
        ca, sa = math.cos(angle), math.sin(angle)
        # 花瓣局部坐标：底部离圆心留 2px 空档，中段略宽，尖端钝化
        gap = 0.6 if solid else 2.0
        local = [(0, -gap), (2.8, -6.0), (2.8, -10.2), (0, -11.5), (-2.8, -10.2), (-2.8, -6.0)]
        pts = []
        for lx, ly in local:
            pts.extend((cx + lx * ca - ly * sa, cy + lx * sa + ly * ca))
        if solid:
            canvas.create_polygon(*pts, smooth=True, splinesteps=12,
                                  fill=color, outline=color, width=1)
        else:
            canvas.create_line(*pts, smooth=True, splinesteps=12, fill=color,
                               width=width, joinstyle='round', capstyle='round')
    # 中心点只在 26px 以上画：小尺寸下它会把五个花瓣糊成一团红点
    if filled_center and not solid:
        canvas.create_oval(cx - 1.8 * k, cy - 1.8 * k, cx + 1.8 * k, cy + 1.8 * k,
                           fill=color, outline=color)


class Ornament(ttk.Canvas):
    """把一枚装饰包成可直接 pack 的控件。

    颜色存的是令牌名（如 'text_muted'）而不是色值 —— 切主题时 `redraw()`
    重新查一次令牌就换过来了，不需要外部记着原来用的是哪个颜色。
    """

    def __init__(self, master, kind='bell', size=26, token='text_muted',
                 background=None, **kwargs):
        bg = background or tokens.color('window')
        pad = 2
        side = size + pad * 2
        super().__init__(master, width=side, height=side, highlightthickness=0,
                         borderwidth=0, background=bg, **kwargs)
        self._kind = kind
        self._size = size
        self._token = token
        self.redraw()

    def redraw(self, color=None):
        """color 只用于一次性覆盖；传 None 就按当前令牌取色。"""
        self.delete('all')
        size = self._size
        pad = 2
        cx = cy = pad + size / 2
        if color is None:
            color = tokens.color(self._token)
        if self._kind == 'bell':
            draw_bell(self, size, color, pad, pad)
        elif self._kind == 'petal':
            draw_petal(self, size, color, pad, pad)
        else:
            draw_flower(self, size, color, cx, cy)
