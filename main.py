"""启动入口：python main.py [--port 8848] [--host 127.0.0.1] [--no-browser]"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.server import serve  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bili-live-wall", description="B 站直播监控墙")
    parser.add_argument("--root", default=ROOT, help="项目根目录（配置文件与截图都放这里）")
    parser.add_argument("--host", default=None, help="覆盖监听地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖端口")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--verbose", action="store_true", help="打印 HTTP 访问日志")
    args = parser.parse_args(argv)

    return serve(
        root=args.root,
        host=args.host,
        port=args.port,
        verbose=args.verbose,
        open_browser=False if args.no_browser else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
