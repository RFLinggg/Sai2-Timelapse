"""生成 Sai2-Timelapse 的 .ico 图标（纯 Pillow 绘制，不依赖外部素材）。

**已被 tools/icon_from_image.py 取代（2026-11-18）。** 现在实际在用的图标是
位图版：`assets/icon-source.png` → `tools/icon_from_image.py` → `Sai2-Timelapse.ico`。
本脚本保留是因为它记录了「深色底 + 竖向渐变铃铛 + 红笔尖」那套几何和配色的推导，
画出来的旧图标仍然可用；但默认拒绝覆盖现有 .ico，防止手滑把新图标换回去。

下面是原始设计说明（旧版）：

构图是「铃铛 + 画笔」并排：铃铛是程序一直在用的母题（🔔），画笔交代
这个程序是干什么的（SAI2 绘画过程录制）。配色直接取 `webui/style.css` 的令牌：

  · 深色圆角底 —— `--panel` 到 `--sunken` 那一档，白底黑底上都能立住；
  · 铃铛**实心**，纵向渐变从顶端冷灰经中段偏暖到裙边/铃舌的红，
    就是 appbar 那颗 22px 铃铛的色带（#ccd2d8 → #a2908c → #c25b54）；
  · 画笔杆/箍走中性色（和铃铛上半段同一族），笔毛用红 —— 红在图标里
    只留两个来源：铃铛下端和笔尖（= 颜料），别处不加；
  · 不画描边版 —— 16px 上 1.5 的描边不到一个像素，直接糊成一团。
    「小尺寸必须填实」这条在界面上验证过，图标只会更极端。

画法是先按 RENDER 尺寸超采样，再逐档 LANCZOS 降采样：
直接在小画布上用 polygon 画，边缘会有明显锯齿。
**每个尺寸单独出图**，不是一张缩到底：
  · 16px 不画铃舌（和裙边只差半个像素，会糊成一根挂下来的尾巴）；
  · 16px 也不画画笔（两个图形挤进 16px 必然糊成一片，实测已确认）。

用法：
    python tools/make_icon.py            # 输出到项目根 Sai2-Timelapse.ico
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / 'Sai2-Timelapse.ico'

RENDER = 1024          # 超采样尺寸
TILE_RADIUS = 0.22     # 圆角占边长的比例

# ---- 颜色（与 style.css 对齐）----
TILE_TOP = (35, 41, 47)        # 比 --panel #181b1f 亮一档，深色任务栏上才不会糊成一块
TILE_BOTTOM = (11, 13, 16)     # --sunken #0c0e10 附近
BELL_STOPS = [
    (0.00, (210, 216, 222)),   # --bell-0 附近，顶端中性偏亮
    (0.45, (176, 154, 149)),   # 中段偏暖，但不往脏褐色走
    (0.78, (194, 91, 84)),     # --bell-2 #c25b54：让红提前到裙边就开始
    (1.00, (208, 85, 75)),     # 铃舌收得更红一点，小尺寸才看得出是"那个红"
]

# 画笔：杆和箍走中性色（和铃铛上半段同一族），笔毛用红 = 颜料。
BRUSH_HANDLE = (216, 221, 226, 255)
BRUSH_FERRULE = (138, 145, 152, 255)
BRUSH_BRISTLE = (200, 90, 82, 255)

# ---- 铃铛几何：直接搬 i-bell-solid 的 24 单位路径 ----
# 铃身：顶部圆肩 → 直筒 → 外撇裙边；顶钮一个圆角矩形；铃舌一个实心圆。
BODY_START = (12.0, 3.0)
BODY_CURVE_L = [((12.0, 3.0), (9.0, 3.0), (7.2, 5.4), (7.2, 8.2))]
BODY_LINE = [(7.2, 13.0), (5.4, 16.0), (18.6, 16.0), (16.8, 13.0), (16.8, 8.2)]
BODY_CURVE_R = [((16.8, 8.2), (16.8, 5.4), (15.0, 3.0), (12.0, 3.0))]
KNOB = (11.0, 1.0, 13.0, 3.9)      # x0, y0, x1, y1 —— 比 UI 版略粗，缩到 16px 才看得见
CLAPPER = (12.0, 18.9, 2.0)        # cx, cy, r

GLYPH_TOP, GLYPH_BOTTOM = 1.0, 20.9

# ---- 并排构图：两个图形各自给中心点（画布比例，0..1）----
# 铃铛略偏右、画笔占左边，两者之间留一条缝 —— 32px 上不留缝就会粘成一个色块。
BELL_CX = 0.660
BRUSH_CX = 0.296
BRUSH_CY = 0.500

# 画笔自身：长度/粗细都是画布比例，angle 是顺时针为正的 Pillow 旋转角，
# 负值 = 笔尖朝左下（画笔自然握持的方向）。
BRUSH_ANGLE = -24.0
BRUSH_LENGTH = 0.66
BRUSH_WIDTH = 0.091

# 笔杆 60% / 金属箍 13% / 笔毛 27% 的长度分配
BRUSH_FERRULE_AT = 0.60
BRUSH_BRISTLE_AT = 0.73

# 尺寸 → (铃铛占画布高度, 是否画铃舌, 是否画画笔)。
# 小尺寸要把图形放大才看得清；16px 铃舌和裙边之间只剩半个像素会糊成尾巴，
# 铃铛和画笔也挤不下两个形状，所以 16px 只留铃铛。
PER_SIZE = {
    256: (0.580, True,  True),
    128: (0.580, True,  True),
    64:  (0.580, True,  True),
    48:  (0.600, True,  True),
    32:  (0.620, True,  True),
    16:  (0.800, False, False),
}


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(stops, t):
    """在 stops 上取 t（0..1）处的颜色。"""
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t0 <= t <= t1:
            span = t1 - t0 or 1
            return lerp(c0, c1, (t - t0) / span)
    return stops[-1][1]


def vertical_gradient(size: int, stops) -> Image.Image:
    img = Image.new('RGB', (size, size))
    px = img.load()
    for y in range(size):
        c = gradient(stops, y / max(size - 1, 1))
        for x in range(size):
            px[x, y] = c
    return img


def cubic(p0, p1, p2, p3, n=64):
    """采样三次贝塞尔。铃肩那段必须采样，直接用折线会有明显的棱角。"""
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        pts.append((
            u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1],
        ))
    return pts


def bell_outline() -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for seg in BODY_CURVE_L:
        pts += cubic(*seg)
    pts += BODY_LINE
    for seg in BODY_CURVE_R:
        pts += cubic(*seg)
    return pts


def tile(size: int) -> Image.Image:
    """圆角方块底 + 竖向渐变。"""
    base = vertical_gradient(size, [(0.0, TILE_TOP), (1.0, TILE_BOTTOM)]).convert('RGBA')
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * TILE_RADIUS), fill=255)
    base.putalpha(mask)
    return base


def rim_layer(size: int) -> Image.Image:
    """极淡的内描边：深色任务栏上给方块一个轮廓，不然整块融进背景。"""
    rim = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle(
        [1, 1, size - 2, size - 2], radius=int(size * TILE_RADIUS),
        outline=(255, 255, 255, 22), width=max(1, int(size * 0.005)))
    return rim


def bell_layer(size: int, ratio: float, clapper: bool, cx: float = 0.5) -> Image.Image:
    """铃铛单独成层（透明底），方便和画笔并排。

    cx 是图形中心的横向位置（画布比例）。纵向一律居中。

    不画铃舌时，图形的下边界要收到裙边（y=16）—— 否则会按含铃舌的高度居中，
    结果整只铃铛偏上、底下空一大块（16px 帧踩过这个坑）。
    """
    glyph_top = GLYPH_TOP
    glyph_bottom = GLYPH_BOTTOM if clapper else 16.0
    glyph_h = glyph_bottom - glyph_top
    scale = size * ratio / glyph_h
    top = (size - glyph_h * scale) / 2
    center_x = size * cx

    def T(p):
        return ((p[0] - 12.0) * scale + center_x, (p[1] - glyph_top) * scale + top)

    shape = Image.new('L', (size, size), 0)
    d = ImageDraw.Draw(shape)
    d.polygon([T(p) for p in bell_outline()], fill=255)

    kx0, ky0, kx1, ky1 = KNOB
    d.rounded_rectangle([T((kx0, ky0)), T((kx1, ky1))],
                        radius=int((ky1 - ky0) * scale * 0.5), fill=255)

    if clapper:
        ccx, ccy, cr = CLAPPER
        ccx, ccy = T((ccx, ccy))
        d.ellipse([ccx - cr * scale, ccy - cr * scale,
                   ccx + cr * scale, ccy + cr * scale], fill=255)

    # 色带按**铃铛自身的高度**铺，而不是画布高度 —— 否则上下留白会吃掉
    # 两端的中性色和红，铃铛看着像整块都偏灰。
    fill = Image.new('RGB', (size, size))
    px = fill.load()
    y0 = top
    y1 = top + glyph_h * scale
    for y in range(size):
        c = gradient(BELL_STOPS, (y - y0) / max(y1 - y0, 1))
        for x in range(size):
            px[x, y] = c

    out = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    out.paste(fill, (0, 0), shape)
    return out


def brush_layer(size: int, cx: float = BRUSH_CX, cy: float = BRUSH_CY,
                length: float = BRUSH_LENGTH, width: float = BRUSH_WIDTH,
                angle: float = BRUSH_ANGLE) -> Image.Image:
    """一支斜着的画笔：笔杆在上、笔毛（红）在下。

    先在画布正中竖着画，绕自身中心旋转，再整体平移到 (cx, cy)。
    先竖后转，比直接算斜线坐标好写，调角度也只改一个数。
    """
    L = size * length
    w = size * width
    layer = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    mx = size / 2
    top = (size - L) / 2
    bottom = top + L
    ferrule_top = top + L * BRUSH_FERRULE_AT
    bristle_top = top + L * BRUSH_BRISTLE_AT

    # 比例按"细杆 + 宽笔头"来：杆细、箍略宽、笔毛再宽一圈收成尖，
    # 这样才像画笔。三者等宽的话读出来是一根白棍子。
    w_handle = w * 0.62
    w_ferrule = w * 1.00
    w_bristle = w * 1.28

    # 笔杆：上端圆头
    d.rounded_rectangle([mx - w_handle / 2, top, mx + w_handle / 2, ferrule_top],
                        radius=w_handle / 2, fill=BRUSH_HANDLE)
    # 金属箍：压一道深色把杆和毛分开
    d.rectangle([mx - w_ferrule / 2, ferrule_top, mx + w_ferrule / 2, bristle_top],
                fill=BRUSH_FERRULE)
    # 笔毛：先外扩再向下收成尖
    d.polygon([(mx - w_bristle / 2, bristle_top), (mx + w_bristle / 2, bristle_top),
               (mx + w_bristle * 0.34, bristle_top + (bottom - bristle_top) * 0.45),
               (mx + w_bristle * 0.16, bottom), (mx - w_bristle * 0.16, bottom),
               (mx - w_bristle * 0.34, bristle_top + (bottom - bristle_top) * 0.45)],
              fill=BRUSH_BRISTLE)

    layer = layer.rotate(angle, resample=Image.BICUBIC, center=(mx, size / 2))

    out = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    out.paste(layer, (round(cx * size - size / 2), round(cy * size - size / 2)))
    return out


def build(size: int = RENDER, ratio: float = 0.58, clapper: bool = True,
          brush: bool = True) -> Image.Image:
    base = tile(size)
    if brush:
        # 画笔先贴、铃铛后贴：万一两个形状挨上了，铃铛压在上面更像"主体"
        base.alpha_composite(brush_layer(size))
        base.alpha_composite(bell_layer(size, ratio, clapper, cx=BELL_CX))
    else:
        base.alpha_composite(bell_layer(size, ratio, clapper, cx=0.5))
    base.alpha_composite(rim_layer(size))
    return base


def main() -> int:
    # 2026-11-18：图标已换成位图版（tools/icon_from_image.py 从
    # assets/icon-source.png 生成）。这个脚本画的是旧版「深色底 + 竖渐变铃铛」，
    # 跑一次就会把新图标覆盖回去 —— 所以默认拒绝，要覆盖必须显式 --force。
    if OUT.exists() and '--force' not in sys.argv:
        print(f'拒绝覆盖：{OUT} 已存在。')
        print('当前图标由 tools/icon_from_image.py 从 assets/icon-source.png 生成。')
        print('本脚本画的是旧版矢量图标。确认要换回去就加 --force。')
        return 1

    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]

    # 每个尺寸单独出图，不是一张图缩到底 —— 参数见 PER_SIZE
    frames = []
    for w, _h in sizes:
        ratio, clapper, brush = PER_SIZE[w]
        frames.append(build(RENDER, ratio, clapper, brush).resize((w, w), Image.LANCZOS))

    frames[0].save(OUT, format='ICO', sizes=sizes, append_images=frames[1:])
    print(f'wrote {OUT} ({OUT.stat().st_size} bytes)')
    print('sizes:', ', '.join(f'{w}x{h}' for w, h in sizes))

    # 顺便存一份 png 方便预览
    png = OUT.with_suffix('.png')
    frames[0].save(png)
    print(f'wrote {png} (preview)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
