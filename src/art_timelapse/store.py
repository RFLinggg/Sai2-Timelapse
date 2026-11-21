"""设置存储 —— 不依赖 tkinter。

原来 ``Settings`` 建在 ``ttk.StringVar`` 上：变量的 ``trace`` 顺手就把值同步进
``data``，界面上改一个框设置就跟着变。换到 WebView 之后界面里没有 Tk 变量了，
但**配置文件必须完全兼容**（``~/.art-timelapse``，键名、默认值、类型都不能变），
否则老用户的帧目录、导出设置、窗口位置全丢。

所以这里只保留"值 + 默认值 + 类型"三件事，取值走 ``get`` / ``set``。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path


# 配置结构版本。加一次迁移就 +1，并在 ``migrate`` 里补一段 ``if version < N``。
SETTINGS_VERSION = 4

# 窗口高度超过这个值，就认为是旧版「展开高级设置时把窗口撑高」留下的尺寸。
# 1024 高的屏幕会被撑到 ~1010，900 高的屏幕到 ~890 —— 都远在 820 之上。
WIN_HEIGHT_SUSPECT = 820
# 修回去的高度。**820 是算出来的，不是拍的**：965 宽时表单全部展开要 478px、
# 日志区下限 120px、上下留白 20px，再加上顶栏 / 页签 / 页脚（44+36+48），
# 窗口得 820 才够 —— 低于这个数，展开面板就得靠页面滚动，而滚动必然把某一
# 行切在边上（实测 728 时主按钮正好被切掉一半）。1080p 的工作区是 1032，
# 820 放得下；屏幕更小的机器会被系统夹住，那时退回滚动，不会崩。
WIN_HEIGHT_FIXED = 820


def settings_version(data: dict) -> int:
    """读 ``settings_version``，脏值一律当 0 处理。"""
    try:
        return int(data.get('settings_version') or 0)
    except (TypeError, ValueError):
        return 0


def migrate(data: dict):
    """把老配置里「还停在旧默认值上」的项迁到新默认值。

    只动**仍等于旧默认值**的项 —— 用户明确选过的值不碰。这条规矩很重要：
    比如录制分辨率从 1000 改成 1920，1000 不在任何预设档位上，还停在这个值
    说明从没手动改过；但 3840 是用户自己选的 4K，不能动。

    返回值：``None`` 表示已经是最新版本、什么都没做；否则返回 ``{key: 新值}``
    （可能是空字典，表示版本号推进了但没有要改的值）。调用方拿 ``None`` 判断
    要不要落盘 —— **版本号必须写回磁盘**，否则每次启动都会重跑一遍，
    用户手动把值改回旧默认值之后还会被再改一次。

    放在存储层而不是各界面里：WebView 版和 Tk 版共用同一个
    ``~/.art-timelapse``，两边各写一份迁移迟早会走岔。
    """
    version = settings_version(data)
    if version >= SETTINGS_VERSION:
        return None
    changes = {}
    if version < 2:
        # 旧版导出时长默认 60 秒，等于「录 3 小时只出 60 秒」，界面还不提示。
        if data.get('export_time_limit') == 60.0:
            changes['export_time_limit'] = 0.0
    if version < 3:
        # 录制分辨率默认 1000px → 1920px（1080p）。缩时回放成片 1000px 太糊，
        # 而 1000 这个数当初只是随手写的，不是任何档位。
        if data.get('image_size_limit') == 1000:
            changes['image_size_limit'] = 1920
    if version < 4:
        # 这是**修数据**，不是改默认值：旧版一点「高级设置」就把窗口撑高，
        # 关窗时又把撑大的尺寸写回 win_geometry。窗口改成固定尺寸之后，
        # 那份被撑到贴着屏幕底边的尺寸会把新版本的第一印象直接毁掉。
        # 只在明显是"被撑大"（> 820）时收一次，之后用户拖成多少都不再干预。
        geom = str(data.get('win_geometry') or '')
        match = re.match(r'^(\d+)x(\d+)(.*)$', geom)
        if match and int(match.group(2)) > WIN_HEIGHT_SUSPECT:
            width = int(match.group(1))
            changes['win_geometry'] = f'{width}x{WIN_HEIGHT_FIXED}{match.group(3)}'
            changes['win_size'] = [width, WIN_HEIGHT_FIXED]
    data.update(changes)
    data['settings_version'] = SETTINGS_VERSION
    return changes


class Store:
    """一个很薄的类型化 JSON 配置。

    类型以 ``defaults`` 里的默认值为准：读盘时按默认值的类型强转，
    避免旧文件里把 ``"30"`` 写成字符串、或者把 ``0`` 写成 ``false`` 之后
    下游 ``int()`` 炸掉。
    """

    def __init__(self, file_name: str, defaults: dict):
        self.file_path = Path.home() / file_name
        self.defaults = dict(defaults)
        self.data = dict(defaults)
        self._load()

    # ------------------------------------------------------------ 磁盘
    def _load(self):
        if self.file_path.exists():
            try:
                # 显式 utf-8：从资源管理器启动时默认编码是 cp936，
                # 中文路径存进去再读出来就乱码了。
                with self.file_path.open(encoding='utf-8') as fp:
                    saved = json.load(fp)
            except Exception:
                logging.exception('Could not read settings, using defaults')
                saved = {}
            for key, value in saved.items():
                if key in self.defaults:
                    self.data[key] = self._coerce(key, value)
                else:
                    # 未知键（老版本留下的、或主题/窗口这类只在 Tk 界面用的）原样留着，
                    # 退出时写回去，别把用户的其它配置吃掉。
                    self.data[key] = value
        # 旧默认值 → 新默认值。落盘一次把版本号钉住，别每次启动都重跑。
        if migrate(self.data) is not None:
            self.save()

    def save(self):
        try:
            with self.file_path.open('w', encoding='utf-8') as fp:
                json.dump(self.data, fp, ensure_ascii=False)
        except Exception:
            logging.exception('Could not save settings')

    # ------------------------------------------------------------ 取值
    def _coerce(self, key, value):
        default = self.defaults[key]
        try:
            if isinstance(default, bool):
                # bool 要单独判：int(True) 会变成 1，写回 JSON 就不是布尔了
                if isinstance(value, str):
                    return value.strip().lower() in ('1', 'true', 'yes', 'on')
                return bool(value)
            if isinstance(default, int):
                return int(float(value))
            if isinstance(default, float):
                return float(value)
            if isinstance(default, str):
                return '' if value is None else str(value)
        except (TypeError, ValueError):
            logging.warning('Bad setting %s=%r, falling back to default', key, value)
            return default
        return value

    def get(self, key):
        return self.data.get(key, self.defaults.get(key))

    def set(self, key, value, save=True):
        if key in self.defaults:
            value = self._coerce(key, value)
        self.data[key] = value
        if save:
            self.save()
        return value

    def update(self, patch: dict):
        """批量写。界面上一次改多个字段时用，只落一次盘。"""
        for key, value in patch.items():
            self.set(key, value, save=False)
        self.save()

    def all(self) -> dict:
        return dict(self.data)

    def reset(self, keep=()) -> list:
        """恢复默认值，保留 ``keep`` 里的键（帧目录、主题、语言、窗口位置）。

        返回被重置的键名，界面上可以据此提示「恢复了 N 项」。
        """
        restored = []
        for key, default in self.defaults.items():
            if key in keep:
                continue
            if self.data.get(key) != default:
                self.data[key] = default
                restored.append(key)
        self.save()
        return restored
