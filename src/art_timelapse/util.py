"""与界面无关的小工具。Tk 版和 WebView 版共用，避免两边各抄一份。"""
from __future__ import annotations


def clamp_number(value, low, high, default):
    """把用户输入夹到合法范围；转不成数字（或 NaN）就用默认值。

    在「读取处」夹紧而不是每敲一个键就纠正输入框 —— 后者会在用户还没输完时
    就把内容改掉（想输 10，刚敲 1 就被夹成别的值）。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return min(max(number, low), high)


def format_duration(seconds: float) -> str:
    """把秒数格式化成 ``m:ss`` / ``h:mm:ss``。"""
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'
