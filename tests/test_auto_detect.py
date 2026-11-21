"""auto_detect 的离机测试：参数校验、缓存合并、超时、错误不崩。

    venv\\Scripts\\python.exe tests\\test_auto_detect.py

不需要 SAI2、不需要画布。真画布上的三层命中见 ``test_detect_live.py``。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

from art_timelapse import auto_detect as ad  # noqa: E402

fails = []


def check(name, cond, extra=''):
    print(f'{"PASS" if cond else "FAIL"}  {name}' + (f'  {extra}' if extra else ''))
    if not cond:
        fails.append(name)


def test_hash_guard():
    for bad in ('', '0' * 32, 'zz' * 16, None, 'abc'):
        res = ad.detect(999999, bad, 'x', log=lambda m: None, timeout=1.0)
        check(f'非法 MD5 被拒: {bad!r}', not res['ok'] and 'MD5' in res['message'],
              res['message'])
    # 合法 hash 但进程不存在 -> 失败，不抛
    res = ad.detect(999999, 'a' * 32, 'x', log=lambda m: None, timeout=1.0)
    check('进程不存在时不崩', not res['ok'], res['message'])


def test_cache_merge():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / 'versions.json'
        old = ad.CACHE_PATH
        ad.CACHE_PATH = path
        try:
            path.write_text(json.dumps({
                'schema': 1,
                'versions': [
                    {'hash': 'a' * 32, 'name': 'A', 'offset': '0x320220', 'layout': 'base'},
                    {'hash': 'b' * 32, 'name': 'B', 'offset': '0x478280', 'layout': 'new'},
                ],
            }, ensure_ascii=False), encoding='utf-8')

            ad.save_cache('c' * 32, 'C', 0x321620, 'base')
            data = json.loads(path.read_text(encoding='utf-8'))
            hashes = [v['hash'] for v in data['versions']]
            check('追加不动已有条目', hashes == ['a' * 32, 'b' * 32, 'c' * 32], hashes)

            ad.save_cache('a' * 32, 'A2', 0x320420, 'base')
            data = json.loads(path.read_text(encoding='utf-8'))
            hashes = [v['hash'] for v in data['versions']]
            entry = next(v for v in data['versions'] if v['hash'] == 'a' * 32)
            check('同 hash 覆盖而不是重复', hashes.count('a' * 32) == 1, hashes)
            check('覆盖后 offset 更新', entry['offset'] == '0x320420', entry)

            # new 布局要带 fields，否则重启后加载出来是花屏
            ad.save_cache('d' * 32, 'D', 0x478280, 'new')
            data = json.loads(path.read_text(encoding='utf-8'))
            entry = next(v for v in data['versions'] if v['hash'] == 'd' * 32)
            check('new 布局写了 fields', isinstance(entry.get('fields'), list)
                  and len(entry['fields']) >= 10, len(entry.get('fields') or []))

            # 坏文件：不应抛，且原文件留档
            path.write_text('{not json', encoding='utf-8')
            ad.save_cache('e' * 32, 'E', 0x320720, 'base')
            data = json.loads(path.read_text(encoding='utf-8'))
            check('坏文件被重建', [v['hash'] for v in data['versions']] == ['e' * 32])
            check('坏文件留了档', (Path(td) / 'versions.json.bad').exists())
        finally:
            ad.CACHE_PATH = old


def test_reload_roundtrip():
    """写进缓存的东西，必须能被主程序的外部表加载器读回来。"""
    from art_timelapse import sai
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / 'versions.json'
        old = ad.CACHE_PATH
        ad.CACHE_PATH = path
        try:
            ad.save_cache('f' * 32, 'Round Trip', 0x478280, 'new')
            n = sai.load_external_versions([path])
            cls = sai.sai_api_lookup.get('f' * 32)
            check('外部表能载入探测写出的条目', n == 1 and cls is not None, n)
            check('载入后 offset 正确', cls and cls.session_offset == 0x478280)
            check('new 布局载入后也覆写了画布存活判定',
                  cls and 'check_if_canvas_exists' in cls.__dict__)
            sai.load_external_versions([])
            sai.sai_api_lookup.pop('f' * 32, None)
        finally:
            ad.CACHE_PATH = old


def test_verify_deadline():
    """坏 proc 不能让 verify 抛出去 —— 探测是在后台线程跑的，抛了会静默卡住任务。"""
    class Dead:
        base_address = 0
    t0 = time.time()
    res = ad.verify(Dead(), 0x321620, 'base', 'a' * 32, 'x',
                    deadline=time.time() - 1)
    check('坏进程返回失败而不是抛异常', not res['ok'], res['error'])
    check('没有卡住', time.time() - t0 < 2.0, f'{time.time() - t0:.2f}s')


def main():
    test_hash_guard()
    test_cache_merge()
    test_reload_roundtrip()
    test_verify_deadline()
    print()
    if fails:
        print(f'{len(fails)} 项失败: ' + ', '.join(fails))
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
