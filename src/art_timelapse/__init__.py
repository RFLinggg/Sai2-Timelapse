# ---------------------------------------------------------------------------
# Modified from art-timelapse -- https://github.com/cromachina/art-timelapse
# Copyright (C) 2024-2026 cromachina. Licensed under GPL-3.0 (see LICENSE).
#
# 本文件在上游基础上做过修改：版本号、窗口标题、多语言初始化、日志路径
# 修改自 2026-11-19 起，逐次记录见 适配说明.md。
# ---------------------------------------------------------------------------
import logging
import gettext
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------- 程序身份
# 名字分两种写法，是有意的：
#   APP_TITLE  用户看得见的地方（窗口标题、exe 属性、快捷方式）——带铃铛
#   APP_NAME   文件名 / 互斥量 / 类名——不带 emoji。emoji 在文件名和
#              taskkill / cmd 里能跑，但被编码问题咬的概率远高于收益。
APP_NAME = 'Sai2-Timelapse'
APP_TITLE = 'Sai2-Timelapse🔔'
__version__ = '1.34'

# 配置文件**不能跟着改名**：老用户的帧目录、导出设置、窗口位置全在这里，
# 换个文件名等于让他们重配一遍。所以名字锁死在 .art-timelapse。
SETTINGS_FILE = '.art-timelapse'


def window_title() -> str:
    """窗口标题。两套界面（WebView / Tk）和找窗口的探针都用这一个。"""
    return f'{APP_TITLE} v{__version__}'


def default_log_path() -> Path:
    """打包版没有控制台，日志必须留一份在磁盘上，否则出问题完全没线索。"""
    base = os.environ.get('APPDATA') or str(Path.home())
    return Path(base) / APP_NAME / f'{APP_NAME}.log'

# Set ART_TIMELAPSE_LOG=<path> to override where logs are mirrored.
_log_path = os.environ.get('ART_TIMELAPSE_LOG')
_handlers = None
if _log_path:
    _handlers = [logging.FileHandler(_log_path, encoding='utf-8')]
elif getattr(sys, 'frozen', False):
    try:
        _default_log = default_log_path()
        _default_log.parent.mkdir(parents=True, exist_ok=True)
        _handlers = [RotatingFileHandler(_default_log, maxBytes=1_000_000,
                                         backupCount=2, encoding='utf-8')]
    except OSError:
        # 写不了日志不该让程序起不来
        _handlers = None

logging.basicConfig(
    format='[%(asctime)s][%(levelname)s] %(message)s',
    level=logging.INFO,
    handlers=_handlers,
)

def get_log_path():
    """当前实际写入的日志文件路径；没有落盘时返回 None。"""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, (logging.FileHandler, RotatingFileHandler)):
            return Path(handler.baseFilename)
    return None

lang = None

def get_locales_dir() -> Path:
    """Return the directory holding the compiled .mo catalogs.

    Works both when running from source and when frozen by PyInstaller
    (where resources are unpacked under sys._MEIPASS).
    """
    candidates = []
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidates.append(Path(meipass) / 'art_timelapse' / 'locales')
        candidates.append(Path(meipass) / 'locales')
    candidates.append(Path(__file__).resolve().parent / 'locales')
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[-1]

def get_resource_path(*parts) -> Path:
    """Locate a bundled resource (icon, data file) in both source and frozen runs."""
    meipass = getattr(sys, '_MEIPASS', None)
    pkg_dir = Path(__file__).resolve().parent
    roots = []
    if meipass:
        roots.append(Path(meipass))
    roots.extend([pkg_dir, pkg_dir.parent])
    for root in roots:
        candidate = root.joinpath(*parts)
        if candidate.exists():
            return candidate
    return roots[-1].joinpath(*parts)

def set_locale(locale_code):
    global lang
    path = get_locales_dir()
    try:
        lang = gettext.translation('art-timelapse', localedir=path, languages=[locale_code], fallback=locale_code == 'en')
    except (FileNotFoundError, OSError):
        # Catalog not built for this language: degrade to source strings
        # instead of crashing the whole app.
        lang = gettext.NullTranslations()

set_locale('zh_CN')

def _(text):
    return lang.gettext(text)