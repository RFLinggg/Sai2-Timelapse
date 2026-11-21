"""生成 Sai2-Timelapse 介绍视频的封面（1920×1080）。

版式：左文右图的产品页。左边讲是什么、卖点三条；右边一张真实界面截图，
带柔和投影、右侧出血到画布外沿。背景浅灰 + 极淡点阵 + 右上角径向提亮，
文字用 Noto Sans SC 可变字重（Regular / Medium / Bold 三档）。

截图里「保存位置」那行会露出本地路径，发布前用输入框同色的圆角块盖掉 ——
看起来就是「输入框空着」，比打马赛克干净。

用法：
    python tools/make_cover.py                       # 输出 cover/cover-1920x1080.png
    python tools/make_cover.py --out xxx.png
    python tools/make_cover.py --shot 别的截图.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / 'cover'

W, H = 1920, 1080

# ---- 配色（跟 webui/style.css 的浅色主题同一族，不另创一套） ----
BG = (244, 244, 245)
DOT = (232, 233, 235)
INK = (22, 25, 28)
INK_SOFT = (58, 63, 68)
MUTED = (122, 128, 134)
RED = (196, 82, 74)
PANEL = (255, 255, 255)

FONT_PATH = 'C:/Windows/Fonts/NotoSansSC-VF.ttf'

# 截图里要盖掉的那行文字（原图像素坐标，965×820 那张）
# 裁到状态卡片上边框之前（卡片分隔线在 y=432）：日志正文里会有
# 「热键被占用」这类黄色警告，放进封面很扎眼，而且下面一大片空白。
SHOT_CROP = (2, 33, 963, 394)
# 两处本地路径都要盖：保存位置整行、画布下拉框里括号内的路径。
# 画布那处只盖括号段，前面的文件名留着 —— 盖掉文件名反而像输入框坏了。
PATH_BOX = (158, 132, 648, 162)
CANVAS_BOX = (236, 175, 392, 200)


def F(size: int, weight: str = 'Regular') -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(FONT_PATH, size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass
    return f


def dotted_bg(size: tuple[int, int], step: int = 34, radius: int = 1) -> Image.Image:
    """极淡点阵。产品页常用，但颜色必须够淡，否则抢标题。"""
    im = Image.new('RGB', size, BG)
    d = ImageDraw.Draw(im)
    for y in range(step // 2, size[1], step):
        for x in range(step // 2, size[0], step):
            d.ellipse([x - radius, y - radius, x + radius, y + radius], fill=DOT)
    return im


def radial_glow(size: tuple[int, int], center: tuple[int, int], radius: int,
                strength: int = 26) -> Image.Image:
    """右上角提亮：一层白色径向渐变，让截图那块不那么平。"""
    layer = Image.new('L', size, 0)
    d = ImageDraw.Draw(layer)
    steps = 40
    for i in range(steps, 0, -1):
        r = int(radius * i / steps)
        a = int(strength * (1 - i / steps) ** 1.6)
        d.ellipse([center[0] - r, center[1] - r, center[0] + r, center[1] + r], fill=a)
    white = Image.new('RGB', size, (255, 255, 255))
    return white, layer


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new('L', size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def drop_shadow(im: Image.Image, blur: int = 42, spread: int = 18,
                opacity: int = 48, offset: tuple[int, int] = (0, 26)) -> Image.Image:
    """把阴影单独做一层返回（尺寸 = 原图 + 四周留白），调用方负责合成。"""
    pad = blur * 2 + spread + max(abs(offset[0]), abs(offset[1]))
    canvas = Image.new('L', (im.width + pad * 2, im.height + pad * 2), 0)
    shape = Image.new('L', im.size, 0)
    ImageDraw.Draw(shape).rounded_rectangle(
        [0, 0, im.width - 1, im.height - 1], radius=spread + 6, fill=opacity)
    canvas.paste(shape, (pad + offset[0], pad + offset[1]))
    return canvas.filter(ImageFilter.GaussianBlur(blur)), pad


def load_shot(path: Path, target_w: int) -> tuple[Image.Image, float]:
    """裁掉标题栏、缩放、盖掉本地路径。"""
    im = Image.open(path).convert('RGB')
    im = im.crop(SHOT_CROP)
    scale = target_w / im.width
    im = im.resize((target_w, round(im.height * scale)), Image.LANCZOS)

    # 盖掉路径：输入框和下拉框都是纯白，用同色块覆盖。
    # PATH_BOX / CANVAS_BOX 是原图坐标，必须先减去 SHOT_CROP 的偏移再乘缩放比 ——
    # 否则整块会往下偏一个标题栏的高度，盖到下一行上去（第一版就是这么错的）。
    d = ImageDraw.Draw(im)
    ox, oy = SHOT_CROP[0], SHOT_CROP[1]
    for box, pad_x, pad_y, radius in ((PATH_BOX, 4, 2, 6), (CANVAS_BOX, 3, 3, 4)):
        x0, y0, x1, y1 = box
        d.rounded_rectangle(
            [round((x0 - ox - pad_x) * scale), round((y0 - oy - pad_y) * scale),
             round((x1 - ox + pad_x) * scale), round((y1 - oy + pad_y) * scale)],
            radius=round(radius * scale), fill=(255, 255, 255))
    return im, scale


def draw_rich(d: ImageDraw.ImageDraw, xy: tuple[int, int],
              parts: list[tuple[str, tuple[int, int, int]]],
              font: ImageFont.FreeTypeFont) -> None:
    """一行里混色。中文等宽，但混了英文/数字就得逐段量宽。"""
    x, y = xy
    for text, color in parts:
        d.text((x, y), text, font=font, fill=color, anchor='ls')
        x += d.textlength(text, font=font)


def build(shot_path: Path, out_path: Path) -> None:
    cover = dotted_bg((W, H)).convert('RGB')

    # 右上角提亮
    white, mask = radial_glow((W, H), (1560, 250), 900, 30)
    cover = Image.composite(white, cover, mask)

    d = ImageDraw.Draw(cover)

    # ---------------- 左栏文字 ----------------
    # 整体下移 56：顶部留白 92、底部 202 会显得「内容挂在上面」，
    # 挪完上下各约 148，视觉重心才落在中线附近。
    Y = 56
    x = 132
    icon = Image.open(ROOT / 'assets' / 'icon-source.png').convert('RGBA')
    icon_size = 84
    icon = icon.resize((icon_size, icon_size), Image.LANCZOS)
    icon.putalpha(rounded_mask(icon.size, int(icon_size * 0.22)))
    cover.paste(icon, (x, 92 + Y), icon)

    d.text((x + icon_size + 22, 92 + Y + icon_size // 2), 'Sai2-Timelapse',
           font=F(35, 'Medium'), fill=INK, anchor='lm')
    d.text((x + icon_size + 22, 92 + Y + icon_size // 2 + 32), 'SAI2 绘画过程录制',
           font=F(24, 'Regular'), fill=MUTED, anchor='lm')

    # 主标题两行：「Sai2缩时回放！」「解压即用！」。
    # 比原来那句功能描述短得多，字号可以放到 96（原来压在 84 是为了塞下 8 个汉字）。
    # 红色仍只给「缩时」—— 第二行本来就短，靠字号和位置就够醒目，再加红会散。
    title_y = 300 + Y
    title_font = F(96, 'Bold')
    draw_rich(d, (x, title_y), [
        # 尾随半角空格当混排间隙：「Sai2」直接贴「缩时」会挤在一起
        ('Sai2 ', INK), ('缩时', RED), ('回放！', INK),
    ], title_font)
    draw_rich(d, (x, title_y + 116), [('解压即用！', INK)], title_font)

    # 卖点三条
    bullets = [
        ('直接读画布内存', '，不是录屏'),
        ('画多少笔存多少帧', '，停笔不计时长'),
        ('Ctrl+Alt+S 全局启停', '，不用切窗口'),
    ]
    bullet_font = F(32, 'Regular')
    y = 604 + Y
    for head, tail in bullets:
        # 红色短横做项目符号，比圆点更贴「笔触」的意象
        d.rounded_rectangle([x, y - 14, x + 18, y - 10], radius=2, fill=RED)
        draw_rich(d, (x + 34, y), [(head, INK_SOFT), (tail, MUTED)], bullet_font)
        y += 62

    # 页脚（「免安装」去掉了 —— 标题里的「解压即用」已经说过一遍）
    d.text((x, 878 + Y), 'Windows 10 / 11 · 导出 mp4 / webm',
           font=F(24, 'Regular'), fill=MUTED, anchor='ls')

    # 一条极细分隔线，把文字区和页脚分开
    d.rectangle([x, 836 + Y, x + 260, 837 + Y], fill=(220, 221, 223))

    # ---------------- 右侧截图 ----------------
    # 不出血：这张截图右侧正好是画布预览卡片，往右偏出就会把它切掉一半。
    # 换个思路让画面有呼吸 —— 放大到 1000 宽、右边只留 50，靠尺寸而不是裁切。
    shot, scale = load_shot(shot_path, 1000)
    shot.putalpha(rounded_mask(shot.size, 14))

    shadow, pad = drop_shadow(shot, blur=46, spread=16, opacity=52, offset=(0, 26))
    shot_x, shot_y = 870, (H - shot.height) // 2 - 12
    shadow_rgb = Image.new('RGB', shadow.size, (18, 20, 22))
    cover.paste(shadow_rgb, (shot_x - pad, shot_y - pad), shadow)
    cover.paste(shot, (shot_x, shot_y), shot)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cover.save(out_path)
    print(f'wrote {out_path} ({cover.width}×{cover.height})')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--shot', default=str(ROOT / 'probe' / 'feedback_ui.png'))
    ap.add_argument('--out', default=str(OUT_DIR / 'cover-1920x1080.png'))
    args = ap.parse_args()
    build(Path(args.shot), Path(args.out))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
