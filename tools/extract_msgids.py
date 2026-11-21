"""Extract translatable strings from Sai2-Timelapse sources and compare with a .po file.

Usage:
    python tools/extract_msgids.py [--po path/to/xx.po]
"""
import re
import sys
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / 'src' / 'art_timelapse'

# matches _('...') and _("..."), honouring backslash escapes
CALL_RE = re.compile(r"""_\(\s*(['"])((?:[^'"\\]|\\.)*)\1\s*\)""")
PO_ID_RE = re.compile(r'^msgid "((?:[^"\\]|\\.)*)"', re.M)


def unescape(s: str) -> str:
    return (s.replace('\\n', '\n').replace('\\t', '\t')
             .replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\'))


def collect_source_strings():
    found = {}
    for f in sorted(SRC.glob('*.py')):
        text = f.read_text(encoding='utf-8')
        for m in CALL_RE.finditer(text):
            s = unescape(m.group(2))
            found.setdefault(s, []).append(f.name)
    return found


def collect_po_ids(path):
    text = pathlib.Path(path).read_text(encoding='utf-8')
    ids = set()
    for m in PO_ID_RE.finditer(text):
        ids.add(unescape(m.group(1)))
    ids.discard('')
    return ids


def main():
    po_path = None
    if '--po' in sys.argv:
        po_path = sys.argv[sys.argv.index('--po') + 1]

    found = collect_source_strings()
    print(f'source strings: {len(found)}')

    if po_path:
        ids = collect_po_ids(po_path)
        print(f'po msgids:      {len(ids)}')
        missing = sorted(set(found) - ids)
        stale = sorted(ids - set(found))
        print(f'\n=== in source, missing from po ({len(missing)}) ===')
        for s in missing:
            print('  +', repr(s))
        print(f'\n=== in po, not found in source ({len(stale)}) ===')
        for s in stale:
            print('  -', repr(s))
    else:
        print('\n=== all source strings ===')
        for s in sorted(found):
            print('  ', repr(s))


if __name__ == '__main__':
    main()
