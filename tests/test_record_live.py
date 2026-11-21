"""落笔录制实机测试：确认录制不会「一落笔就停」。

    :: SAI2 里先开一张可以随便动的画
    venv\\Scripts\\python.exe tests\\test_record_live.py

这条路径以前会挂：录制每抓一帧都会调 ``api.check_if_canvas_exists(canvas)``，
外部表动态造出来的 new 布局类没有 id 字段，老代码比 ``c.id`` 直接
``AttributeError``，表现就是点一下笔录制就停。脚本会把本机这个 exe 从版本表
里摘掉再启动主程序，强制走自动适配 —— 命中后注册的类正是那条路径。

**会画一条短线，测完立刻 Ctrl+Z 撤销。** 跑之前确认当前画布可以随便动。
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))

from art_timelapse import sai  # noqa: E402
import _common  # noqa: E402

HOTKEY = (0x11, 0x12, 0x53)      # Ctrl + Alt + S，与默认设置一致


def press_hotkey():
    u = ctypes.windll.user32
    for vk in HOTKEY:
        u.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    for vk in reversed(HOTKEY):
        u.keybd_event(vk, 0, 0x0002, 0)


def launch_app(exe_hash, frames_dir, log_path):
    """把本机条目摘掉再启动主程序，强制它走自动适配。"""
    code = (
        'import sys;'
        f'sys.path.insert(0, {str(ROOT / "src")!r});'
        'from art_timelapse import sai;'
        f'sai.sai_api_lookup.pop({exe_hash!r}, None);'
        'from art_timelapse.__main__ import main;'
        'sys.exit(main())'
    )
    env = dict(os.environ)
    env['ART_TIMELAPSE_LOG'] = str(log_path)
    env['ART_TIMELAPSE_UI_SCRIPT'] = (
        f'window.pywebview.api.set_settings({{frames_file:"{frames_dir.as_posix()}"}}); "ok"')
    return subprocess.Popen(
        [sys.executable, '-c', code], env=env, cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    pid = _common.find_sai2()
    if not pid:
        print('SAI2 没开着。先打开 SAI2 并开一张画，再跑这个脚本。')
        return 1
    if not _common.require_canvas(pid):
        return 1

    exe_hash = sai.get_exe_hash_by_pid(pid)
    if not exe_hash:
        print('算不出 exe 的 MD5')
        return 1

    tmp = Path(tempfile.mkdtemp(prefix='sai2-rec-test-'))
    frames_dir = tmp / 'frames'
    log_path = tmp / 'app.log'
    cfg = Path.home() / '.art-timelapse'
    cfg_bak = Path.home() / '.art-timelapse.rectest.bak'
    if cfg.exists():
        shutil.copyfile(cfg, cfg_bak)

    proc = launch_app(exe_hash, frames_dir, log_path)
    from pynput.keyboard import Controller as Kb, Key
    from pynput.mouse import Button, Controller as Ms
    kb, mouse = Kb(), Ms()

    try:
        print('等自动适配…')
        for _ in range(30):
            time.sleep(1.0)
            if log_path.exists() and '已自动适配' in log_path.read_text(
                    encoding='utf-8', errors='replace'):
                break
        else:
            print('30 秒内没看到「已自动适配」，日志如下：')
            if log_path.exists():
                print(log_path.read_text(encoding='utf-8', errors='replace')[-1500:])
            return 1

        print('开始录制')
        press_hotkey()
        time.sleep(3)

        hwnd, rect = _common.main_window(pid)
        if hwnd is None:
            print('找不到 SAI2 主窗口')
            return 1
        if not _common.activate(hwnd, rect, mouse):
            print('切不到 SAI2 前台，落笔会打到别的窗口上')
            return 1

        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        print(f'在 ({cx},{cy}) 画一条短线')
        mouse.position = (cx - 60, cy)
        time.sleep(0.2)
        mouse.press(Button.left)
        for dx in range(0, 120, 12):
            mouse.position = (cx - 60 + dx, cy)
            time.sleep(0.03)
        mouse.release(Button.left)
        time.sleep(2.5)

        print('停止录制')
        press_hotkey()
        time.sleep(3)

        print('撤销那一条线')
        _common.activate(hwnd, rect, mouse)
        kb.press(Key.ctrl); kb.press('z'); kb.release('z'); kb.release(Key.ctrl)
        time.sleep(0.6)
    finally:
        subprocess.run(['taskkill', '/F', '/PID', str(proc.pid)], capture_output=True)
        time.sleep(1)
        if cfg_bak.exists():
            shutil.copyfile(cfg_bak, cfg)
            cfg_bak.unlink()

    text = log_path.read_text(encoding='utf-8', errors='replace') if log_path.exists() else ''
    print('\n--- 关键日志 ---')
    for line in text.splitlines():
        if any(k in line for k in ('已自动适配', '录制已', '正在写入', '帧目录',
                                   'Traceback', 'AttributeError', '画布')):
            print(line)

    files = sorted(frames_dir.rglob('*')) if frames_dir.exists() else []
    total = sum(f.stat().st_size for f in files if f.is_file())
    print(f'\n帧目录: {len(files)} 项, {total / 1024:.0f} KB  ({frames_dir})')
    for f in files[:6]:
        print('  ', f.relative_to(frames_dir), f.stat().st_size)

    bad = any(k in text for k in ('AttributeError', 'Traceback'))
    ok = bool(files) and not bad
    print('\n结论:', '落笔没掉线，出帧了' if ok else ('没出帧' if not files else '有异常'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
