"""``python -m art_timelapse`` 的入口。

默认起 WebView 界面；WebView2 不可用时退回 Tk 界面。
和 ``packaging/launcher.py`` 的 ``_run()`` 保持同一套逻辑 ——
两个入口行为不一致的话，"源码版能跑、打包版打不开"这类问题会很难查。
"""
import logging
import sys


def main():
    try:
        from .webapp import main as web_main
        web_main()
        return 0
    except Exception:
        logging.exception('WebView UI failed to start, falling back to the Tk UI')

    from .gui import main as tk_main
    tk_main()
    return 0


if __name__ == '__main__':
    sys.exit(main())
