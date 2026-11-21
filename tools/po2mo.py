"""Minimal pure-Python .po -> .mo compiler.

No external tooling (msgfmt / setuptools-gettext) required. Handles the subset
of the PO format used by Sai2-Timelapse: single and multi-line msgid/msgstr,
backslash escapes, and obsolete (#~) entries (ignored).

Usage:
    python tools/po2mo.py po/zh_CN.po src/art_timelapse/locales/zh_CN/LC_MESSAGES/art-timelapse.mo
"""
import ast
import struct
import sys
import pathlib


def parse_po(text):
    """Return {msgid: msgstr} for all non-obsolete entries."""
    entries = {}
    msgid = msgstr = None
    field = None  # which of msgid/msgstr we are accumulating

    def flush():
        nonlocal msgid, msgstr
        if msgid is not None and msgstr is not None:
            entries[msgid] = msgstr
        msgid = msgstr = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            # comments: a blank line separates entries, but we key on msgid
            # start so it is safe to ignore comments entirely.
            continue
        if line.startswith('msgid '):
            if msgid is not None and msgstr is not None:
                flush()
            msgid = _decode(line[len('msgid '):])
            field = 'id'
        elif line.startswith('msgstr '):
            msgstr = _decode(line[len('msgstr '):])
            field = 'str'
        elif line.startswith('"'):
            chunk = _decode(line)
            if field == 'id':
                msgid = (msgid or '') + chunk
            elif field == 'str':
                msgstr = (msgstr or '') + chunk
        else:
            # msgid_plural / msgstr[n] etc. — not used here
            field = None
    if msgid is not None and msgstr is not None:
        flush()
    return entries


def _decode(literal):
    """Decode a PO string literal (including the surrounding quotes)."""
    literal = literal.strip()
    if not literal:
        return ''
    return ast.literal_eval(literal)


def build_mo(entries):
    """Serialize {msgid: msgstr} to the GNU .mo binary format."""
    items = sorted(entries.items())  # msgids must be sorted (binary search)
    n = len(items)

    # layout: header(28) | msgid table | msgstr table | msgid blob | msgstr blob
    header_size = 28
    table_size = n * 8
    msgid_table_off = header_size
    msgstr_table_off = msgid_table_off + table_size
    strings_off = msgstr_table_off + table_size

    msgid_blob = b''
    msgid_offsets = []
    for mid, _ in items:
        mb = mid.encode('utf-8')
        msgid_offsets.append((len(mb), strings_off + len(msgid_blob)))
        msgid_blob += mb + b'\x00'

    msgstr_blob = b''
    msgstr_offsets = []
    for _, mstr in items:
        sb = mstr.encode('utf-8')
        msgstr_offsets.append((len(sb), strings_off + len(msgid_blob) + len(msgstr_blob)))
        msgstr_blob += sb + b'\x00'

    out = bytearray()
    # magic, revision, count, msgid table offset, msgstr table offset,
    # hash table size, hash table offset
    out += struct.pack('<7I', 0x950412DE, 0, n, msgid_table_off,
                       msgstr_table_off, 0, 0)
    for length, off in msgid_offsets:
        out += struct.pack('<2I', length, off)
    for length, off in msgstr_offsets:
        out += struct.pack('<2I', length, off)
    out += msgid_blob
    out += msgstr_blob
    return bytes(out)


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    po_path = pathlib.Path(sys.argv[1])
    mo_path = pathlib.Path(sys.argv[2])
    entries = parse_po(po_path.read_text(encoding='utf-8'))
    mo_path.parent.mkdir(parents=True, exist_ok=True)
    mo_path.write_bytes(build_mo(entries))
    print(f'{po_path} -> {mo_path}  ({len(entries)} entries, {mo_path.stat().st_size} bytes)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
