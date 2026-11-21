"""按 msgid 清理 .po 里的孤儿条目（源码里已不存在的字符串）。

只做一件事：解析 .po 的条目块，删掉 msgid 在给定集合里的块，其余原样保留。
正确处理多行 msgid（`msgid ""` + 若干续行），这是简易正则做不到的。

用法：
    python tools/prune_po.py po/zh_CN.po --drop-orphans        # 自动对比源码，只打印
    python tools/prune_po.py po/zh_CN.po --drop-orphans --apply # 实际写入
    python tools/prune_po.py po/zh_CN.po --drop "PSD Recording" # 指定删除
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / 'src' / 'art_timelapse'


def unescape(s: str) -> str:
    return (s.replace('\\\\', '\x00').replace('\\"', '"')
             .replace('\\n', '\n').replace('\\t', '\t').replace('\x00', '\\'))


def escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t')


def split_blocks(text: str) -> list[list[str]]:
    blocks, cur = [], []
    for line in text.split('\n'):
        if line.strip() == '':
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def unquote(s: str) -> str:
    """去掉 msgid / 续行两端的双引号并反转义。"""
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return unescape(s[1:-1])
    return unescape(s)


def block_msgid(block: list[str]) -> str | None:
    parts, in_id = [], False
    for line in block:
        if line.startswith('msgid '):
            in_id = True
            parts.append(unquote(line[6:]))
        elif in_id:
            if line.startswith('"'):
                parts.append(unquote(line))
            else:
                break
    return ''.join(parts) if in_id else None


def source_msgids() -> set[str]:
    pat = re.compile(r"_\(\s*(['\"])((?:[^'\"\\]|\\.)*)\1\s*\)")
    found: set[str] = set()
    for f in SRC.glob('*.py'):
        text = f.read_text(encoding='utf-8')
        for m in pat.finditer(text):
            raw = m.group(2)
            if '\\' in raw:
                raw = unescape(raw)
            found.add(raw)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('po', type=Path)
    ap.add_argument('--drop-orphans', action='store_true', help='删除源码里不存在的 msgid')
    ap.add_argument('--drop', action='append', default=[], help='指定要删除的 msgid')
    ap.add_argument('--apply', action='store_true', help='实际写入（默认只打印）')
    opts = ap.parse_args()

    text = opts.po.read_text(encoding='utf-8')
    blocks = split_blocks(text)

    drop: set[str] = set(opts.drop)
    if opts.drop_orphans:
        alive = source_msgids()
        for b in blocks:
            mid = block_msgid(b)
            if mid:  # 跳过 header 的空 msgid
                if mid not in alive:
                    drop.add(mid)

    kept, removed = [], []
    for b in blocks:
        mid = block_msgid(b)
        if mid and mid in drop:
            removed.append(mid)
        else:
            kept.append(b)

    print(f'条目总数: {len(blocks)}   待删: {len(removed)}   保留: {len(kept)}')
    for mid in sorted(removed):
        print('  -', repr(mid))

    if not opts.apply:
        print('\n(--dry-run，未写入；加 --apply 生效)')
        return 0
    if not removed:
        print('\n没有需要删除的条目。')
        return 0

    out = '\n\n'.join('\n'.join(b) for b in kept) + '\n'
    opts.po.write_text(out, encoding='utf-8')
    print(f'\n已写入 {opts.po}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
