"""三层识别实机测试：假 MD5 → 抽掉已知 offset → 逼结构扫描 → 正常查表。

    :: SAI2 里先开一张画
    venv\\Scripts\\python.exe tests\\test_detect_live.py

没开画布时每一层都会失败 —— 那不是 bug，链表里只有占位节点，没有东西可验证。

**不写缓存。** ``ad.detect(..., save=False)``，所以跑完不会往
``%USERPROFILE%\\.art-timelapse-versions.json`` 里塞测试条目。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))

from art_timelapse import auto_detect as ad, sai  # noqa: E402
import _common  # noqa: E402


def run_stage(title, exe_hash, exclude=None, fake_hash=None):
    print('\n' + '=' * 60)
    print(title)
    pid = _common.find_sai2()
    if not pid:
        print('  SAI2 已经关了')
        return False
    probe_hash = fake_hash or exe_hash
    saved = None
    if exclude:
        saved = sai.sai_api_lookup.pop(exclude, None)
        if saved:
            print(f'  临时摘掉 {exclude[:8]}'
                  f'（{saved.version_name} / {hex(saved.session_offset)}）')
    try:
        res = ad.detect(pid, probe_hash, 'probe', log=lambda m: print('   ', m),
                        timeout=25.0, save=False)
    finally:
        if saved is not None:
            sai.sai_api_lookup[exclude] = saved
        # 探测成功会把「假 MD5 -> 真实 offset」注册进表。不清掉的话下一层的
        # 候选池里又会出现这个 offset，第 3 层永远走不到结构扫描。
        if fake_hash:
            sai.sai_api_lookup.pop(fake_hash, None)
    print(f"  结果: {'成功' if res['ok'] else '失败'}  阶段={res['stage']}  "
          f"offset={hex(res['offset']) if res['offset'] else '-'}  "
          f"头={res['head_name']!r}  画布={res['canvas_count']}  "
          f"耗时={res['elapsed']}s")
    print(f"  {res['message']}")
    return bool(res['ok'])


def main():
    pid, started = _common.ensure_sai2()
    if not pid:
        return 1
    print(f'pid={pid}')

    if not _common.require_canvas(pid):
        if started:
            import subprocess
            subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
        return 1

    exe_hash = sai.get_exe_hash_by_pid(pid)
    if not exe_hash:
        print('算不出 exe 的 MD5，后面没法测')
        return 1
    cls = sai.sai_api_lookup.get(exe_hash)
    print(f'exe MD5 {exe_hash} -> '
          f'{cls.version_name + " / " + hex(cls.session_offset) if cls else "未登记"}')

    # 第 2 层：假 MD5 + 已知偏移池，模拟「汉化包改了指纹、结构没动」
    ok2 = run_stage('第 2 层：假 MD5 + 已知偏移池', exe_hash, fake_hash='deadbeef' * 4)

    # 第 3 层：把本机这个条目摘掉，池里就没有它的 offset 了，只能靠结构扫描。
    # 先确认池里真的没有 —— 否则这一层是假通过。
    known_offsets = {c['offset'] for c in ad.known_combos(exclude_hash=exe_hash)}
    own_offset = cls.session_offset if cls else None
    if own_offset and own_offset in known_offsets:
        print(f'\n注意：{hex(own_offset)} 还有别的构建在用，第 3 层'
              f'会走「已知偏移」而不是结构扫描')
    ok3 = run_stage('第 3 层：摘掉本机条目，逼结构扫描', exe_hash, exclude=exe_hash)

    # 第 1 层：恢复后用内置条目直接命中
    ok1 = run_stage('第 1 层：正常查表', exe_hash)

    print('\n' + '=' * 60)
    print(f'第 1 层 {"OK" if ok1 else "FAIL"} / '
          f'第 2 层 {"OK" if ok2 else "FAIL"} / '
          f'第 3 层 {"OK" if ok3 else "FAIL"}')
    if started:
        import subprocess
        subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
        print('已关掉测试启动的 SAI2')
    return 0 if (ok1 and ok2) else 1


if __name__ == '__main__':
    sys.exit(main())
