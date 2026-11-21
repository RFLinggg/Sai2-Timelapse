"""Compile every po/*.po into src/art_timelapse/locales/<lang>/LC_MESSAGES/art-timelapse.mo.

Run this after editing any .po file:

    venv\\Scripts\\python.exe tools\\build_locales.py

The language directory name is taken from the .po filename (e.g. ja_JP.po ->
ja_JP/), matching what gettext expects.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from po2mo import parse_po, build_mo  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
PO_DIR = ROOT / 'po'
OUT_DIR = ROOT / 'src' / 'art_timelapse' / 'locales'


def main():
    pos = sorted(PO_DIR.glob('*.po'))
    if not pos:
        print(f'no .po files in {PO_DIR}')
        return 1
    for po in pos:
        lang = po.stem
        entries = parse_po(po.read_text(encoding='utf-8'))
        target = OUT_DIR / lang / 'LC_MESSAGES' / 'art-timelapse.mo'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(build_mo(entries))
        print(f'  {po.name:14} -> {lang}/LC_MESSAGES/art-timelapse.mo  '
              f'({len(entries)} entries, {target.stat().st_size} bytes)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
