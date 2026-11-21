"""从一张位图生成 Sai2-Timelapse 的 .ico（替代 tools/make_icon.py 的程序化绘制）。

源图的构图已经定稿，这个脚本只做三件事：

1. **抠掉圆角外的纯黑**。源图存成 JPEG，四角是 (0,0,0)，圆角内的底是深灰
   (18,19,23) ~ (28,31,36)。两者差得够开，从四角 flood fill 只吃纯黑，
   圆角内的深色底不会被误伤。
2. **裁到内容边界再补一点边距**。原图四周留白偏大，直接缩放到 32px 会让
   铃铛和画笔挤在中间一小块。按 bbox 裁完补 6% 边距，图形占比约 88%。
3. **逐尺寸 LANCZOS 降采样**，不是一张图缩到底。16px 单独处理：整图缩下去
   画笔只剩一条灰线，所以 16px 用「裁铃铛区域」的版本 ——
   这是 make_icon.py 那条老经验（小尺寸只留铃铛）在位图上的等价做法。

用法：
    python tools/icon_from_image.py --src assets/icon-source.png
    python tools/icon_from_image.py --src ... --preview   # 另存各尺寸放大预览
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

SIZES = [256, 128, 64, 48, 32, 16]
PAD_RATIO = 0.06          # 裁剪后补的边距（占内容边长）
FLOOD_THRESH = 20         # flood fill 容差：内部深灰离纯黑约 35，取 20 不误伤


def _flood_bg_to_alpha(im: Image.Image) -> Image.Image:
    """四角 flood fill，把圆角外的纯黑变透明。"""
    rgba = im.convert('RGBA')
    w, h = rgba.size
    for seed in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        try:
            ImageDraw.floodfill(rgba, seed, (0, 0, 0, 0), thresh=FLOOD_THRESH)
        except Exception:
            pass
    return rgba


def _trim_and_pad(im: Image.Image, pad_ratio: float = PAD_RATIO) -> Image.Image:
    """裁到不透明区域，再补一圈边距，最后补成正方形。"""
    bbox = im.getchannel('A').getbbox()
    if bbox:
        im = im.crop(bbox)
    side = max(im.size)
    pad = int(side * pad_ratio)
    canvas = Image.new('RGBA', (side + pad * 2, side + pad * 2), (0, 0, 0, 0))
    canvas.paste(im, ((canvas.width - im.width) // 2,
                      (canvas.height - im.height) // 2))
    return canvas


def _bell_crop(im: Image.Image, cx: float = 0.66, side_ratio: float = 0.62) -> Image.Image:
    """16px 备选：以铃铛为中心裁一个**正方形**块。

    注意必须从裁切这一步就保证正方形。第一版裁的是竖条（left..right 全高），
    圆角内的深色底也是不透明的，alpha bbox 覆盖整条 → 补正方形补不动 →
    缩到 16×16 被横向压扁，出来是个竖条。
    """
    w, h = im.size
    side = int(min(h, w) * side_ratio)
    cx_px = int(w * cx)
    left = max(0, min(cx_px - side // 2, w - side))
    top = max(0, (h - side) // 2)
    return im.crop((left, top, left + side, top + side))


def _sharpen(im: Image.Image, amount: float = 120) -> Image.Image:
    """小尺寸补偿缩小时的模糊。预设 16px 用，32 不必。"""
    from PIL import ImageFilter
    return im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=amount, threshold=2))


def build(src: Path, out_ico: Path, preview: bool = False) -> None:
    original = _flood_bg_to_alpha(Image.open(src))
    full = _trim_and_pad(original)

    frames = []
    for size in SIZES:
        img = full.resize((size, size), Image.LANCZOS)
        if size <= 16:
            # 16px 用整图：铃铛裁切版会把圆角底一起裁掉，在任务栏上和别的图标
            # 风格对不上（见 _bell_crop 的注释）。模糊用小半径锐化顶一下。
            img = _sharpen(img)
        frames.append(img)

    frames[0].save(out_ico, format='ICO', sizes=[(s, s) for s in SIZES],
                   append_images=frames[1:])
    print(f'wrote {out_ico} ({out_ico.stat().st_size} bytes)')

    png = out_ico.with_suffix('.png')
    frames[0].save(png)
    print(f'wrote {png} (256 preview)')

    if preview:
        prev_dir = ROOT / 'probe'
        prev_dir.mkdir(exist_ok=True)
        for f, size in zip(frames, SIZES):
            # 放大到 256 便于肉眼核对小尺寸的糊度
            zoom = f.resize((256, 256), Image.NEAREST)
            zoom.save(prev_dir / f'icon_{size:03d}.png')
            print(f'  preview icon_{size:03d}.png')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=str(ROOT / 'assets' / 'icon-source.png'))
    ap.add_argument('--out', default=str(ROOT / 'Sai2-Timelapse.ico'))
    ap.add_argument('--preview', action='store_true')
    args = ap.parse_args()
    src = Path(args.src)
    if not src.exists():
        print(f'source not found: {src}')
        return 1
    build(src, Path(args.out), args.preview)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
