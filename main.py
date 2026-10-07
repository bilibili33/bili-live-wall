"""启动入口。

源码运行： python main.py
打包之后： 双击 bili-live-wall.exe
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.paths import data_root  # noqa: E402
from app.server import serve  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bili-live-wall", description="B 站直播监控墙")
    parser.add_argument("--root", default=None,
                        help="存放 config.json 和截图的目录（默认：源码运行时是项目目录，打包后是 exe 所在目录）")
    parser.add_argument("--host", default=None, help="覆盖监听地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖端口")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--verbose", action="store_true", help="打印 HTTP 访问日志")
    args = parser.parse_args(argv)

    # 显式给了 --root 就用它；否则打包后用 exe 所在目录，源码运行用项目目录
    root = os.path.abspath(args.root) if args.root else data_root(ROOT)

    return serve(
        root=root,
        host=args.host,
        port=args.port,
        verbose=args.verbose,
        open_browser=False if args.no_browser else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
