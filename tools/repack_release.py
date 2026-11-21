"""重打 Sai2-Timelapse_v<版本>_win64.zip：单层目录 + 说明书。

版本号只从 ``src/art_timelapse/__init__.py`` 的 ``__version__`` 读。
以前这里写死了 ``v1.0``，改版本时得记得改三个地方（代码、这里、说明书标题），
漏一个就会出现「窗口写 v1.01、压缩包叫 v1.0」。

说明书也从 ``packaging/使用说明.txt`` 读 —— 原来是从 dist 里「用户解压出来的
那份副本」找，dist 一清就没了。
"""
import sys
import zipfile
from pathlib import Path

ROOT = Path('D:/Codex work/art-timelapse')
sys.path.insert(0, str(ROOT / 'src'))
from art_timelapse import __version__        # noqa: E402

VER = f'v{__version__}'
SRC = ROOT / 'dist' / 'Sai2-Timelapse'
README_SRC = ROOT / 'packaging' / '使用说明.txt'
ZP = ROOT / 'dist' / f'Sai2-Timelapse_{VER}_win64.zip'
TOP = f'Sai2-Timelapse_{VER}_win64'

readme = README_SRC.read_text(encoding='utf-8-sig')
print(f'版本: {VER}   说明书来源: {README_SRC}')

# 以下三段是「新功能章节」的兜底追加：源文件里已经有了就跳过。
# 正常情况下应该直接改 packaging/使用说明.txt，这里只防漏。
extra = ''
if '942992768' not in readme:
    extra += (
        '反馈\n'
        '遇到问题、想要新版本，或者你的 SAI2 版本抓不到画布，加 QQ 群 942992768。\n'
        '程序底栏左侧有个「反馈 QQ 群」，点一下就能复制群号。\n'
        '\n'
    )
if '悬浮窗' not in readme:
    extra += (
        '悬浮窗\n'
        '启动后屏幕右下角会出现一条小条：直接开始 / 暂停 / 停止录制，\n'
        '并显示当前是不是在录、录了多久、多少帧。\n'
        '按住条的空白处可以拖到别的位置（会自动记住）。\n'
        '不想要的话，点主界面底栏的「悬浮窗：关」即可隐藏，随时可以再打开。\n'
        '\n'
    )
if '接着上次' not in readme:
    extra += (
        '接着上次继续画\n'
        '默认每次「开始录制」都是一段新的视频，导出只读最新那一段 ——\n'
        '今天画一半、明天再录，会是两支片子。\n'
        '想合成一支，就到「高级设置」里把「接着上次录制」打开，\n'
        '之后每次开始都会写进同一段（日志里会写「继续写入会话…（已有 N 帧）」）。\n'
        '一段做完、导出成功后，可以点成片卡片上的「归档这批帧」，\n'
        '把它移进保存位置下的 archive 文件夹，下一次从零开始。\n'
        '归档只是移动，文件都还在，想找回就挪回去。\n'
        '\n'
    )
if '外部版本表' not in readme:
    extra += (
        '支持更多 SAI2 版本\n'
        '程序目录里有一个 sai-versions.example.json，是「外部版本表」的模板。\n'
        '如果你用的 SAI2 版本提示不认识，可以让作者给你一份 sai-versions.json，\n'
        '放到程序目录（或 C:\\Users\\<你的用户名>\\.art-timelapse-versions.json），\n'
        '重开程序就生效，不用换整个程序。日志里会多一行「外部版本表：载入 N 条」。\n'
        '\n'
    )
if '安全模式启动' not in readme:
    extra += (
        '界面里只有色块、没有文字？\n'
        '双击目录里的「安全模式启动.bat」。它禁用了显卡加速 ——\n'
        '控件都在、唯独文字不显示，基本都是这个原因。\n'
        '如果安全模式也没用，试「备用界面启动.bat」：那是另一套界面，\n'
        '样子朴素，但完全不依赖 WebView2。\n'
        '注意：这两个只是启动参数不同，不会改任何设置，平时正常双击 exe 就行。\n'
        '\n'
    )
if extra:
    if '卸载\n' in readme:
        readme = readme.replace('卸载\n', extra + '卸载\n')
    else:
        readme = readme.rstrip() + '\n\n' + extra
    print('追加了缺失的章节')

# 标题行里的版本号也要跟 __version__ 一致，否则压缩包名对了、说明书还写旧版本
first_line, _, rest = readme.partition('\n')
if first_line.startswith('Sai2-Timelapse v'):
    readme = f'Sai2-Timelapse {VER}（Windows 64 位）' + '\n' + rest

tmp = ROOT / 'dist' / '_repack.zip'
example = ROOT / 'packaging' / 'sai-versions.example.json'

# 两个「安全模式」启动器。界面渲染出问题的人不会开命令行，
# 给他们双击就能用的东西。chcp 65001 让中文注释在 cmd 里不乱码。
SAFE_LAUNCHERS = {
    '安全模式启动.bat': '''@echo off
chcp 65001 >nul
title Sai2-Timelapse 安全模式
echo.
echo   正在以「安全模式」启动…
echo.
echo   适用症状：窗口打开了，输入框和按钮都在，但一个字都看不见。
echo   这个模式关掉了显卡加速，界面可能稍慢，但文字应该能出来。
echo.
set WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--disable-gpu
start "" "%~dp0Sai2-Timelapse.exe"
''',
    '备用界面启动.bat': '''@echo off
chcp 65001 >nul
title Sai2-Timelapse 备用界面
echo.
echo   正在用「备用界面」启动…
echo.
echo   适用症状：安全模式也救不回来。
echo   这是另一套界面，样子朴素，但完全不依赖 WebView2。
echo.
set ART_TIMELAPSE_UI=tk
start "" "%~dp0Sai2-Timelapse.exe"
''',
}

n = 0
with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    # 带 BOM：Windows 记事本打开不乱码
    z.writestr(f'{TOP}/使用说明.txt', ('\ufeff' + readme).encode('utf-8'))
    # 外部版本表模板：用户想自己加版本时改这个文件就行
    if example.exists():
        z.write(example, f'{TOP}/{example.name}')
    for name, text in SAFE_LAUNCHERS.items():
        z.writestr(f'{TOP}/{name}', text.encode('utf-8'))
    for p in sorted(SRC.rglob('*')):
        if p.is_file():
            rel = str(p.relative_to(SRC)).replace('\\', '/')
            z.write(p, f'{TOP}/{rel}')
            n += 1
tmp.replace(ZP)
print(f'repacked: {n} files + 1 readme, {ZP.stat().st_size / 1024 / 1024:.1f} MB')

with zipfile.ZipFile(ZP) as z:
    names = z.namelist()
    print('entries:', len(names))
    print('顶层样例:', names[1] if len(names) > 1 else names)
    html = z.read(f'{TOP}/_internal/art_timelapse/webui/index.html').decode()
    print('含反馈按钮:', 'feedbackBtn' in html)
    txt = z.read(f'{TOP}/使用说明.txt').decode('utf-8-sig')
    print('说明书标题:', txt.splitlines()[0])
    print('说明书含群号:', '942992768' in txt)
    print('套层检查:', not any(x.count(TOP) > 1 for x in names))
