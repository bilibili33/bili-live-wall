"""日志：同时往控制台、日志文件和一个内存环形缓冲里写。

打包版的用户看不到源码，出问题时唯一能依靠的就是控制台和日志文件，
所以这里默认把能记的都记下来。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
LEVEL_NAMES = ("debug", "info", "warn", "error")


def level_value(name, default: int = 20) -> int:
    if isinstance(name, int):
        return name
    return LEVELS.get(str(name or "").strip().lower(), default)


def level_name(value: int) -> str:
    for name, num in LEVELS.items():
        if num == value:
            return name
    return "info"


class LogSink:
    """控制台 + 文件 + 内存缓冲，三处一起写。"""

    def __init__(self, path: str | None = None, console_level="debug",
                 file_level="debug", buffer_size: int = 3000):
        self.path = path
        self.console_level = level_value(console_level, LEVELS["debug"])
        self.file_level = level_value(file_level, LEVELS["debug"])
        self._buf: deque[tuple[float, str, str]] = deque(maxlen=buffer_size)
        self._lock = threading.RLock()
        self._fh = None
        self.file_error = ""
        if path:
            self._open(path)

    # ------------------------------------------------------------------
    def _open(self, path: str) -> None:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._rotate(path)
            self._fh = open(path, "a", encoding="utf-8", buffering=1)
        except OSError as exc:
            self._fh = None
            self.file_error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _rotate(path: str, limit: int = 5 * 1024 * 1024) -> None:
        try:
            if os.path.isfile(path) and os.path.getsize(path) > limit:
                old = path + ".1"
                if os.path.exists(old):
                    os.remove(old)
                os.replace(path, old)
        except OSError:
            pass

    # ------------------------------------------------------------------
    def set_levels(self, console_level=None, file_level=None) -> None:
        with self._lock:
            if console_level is not None:
                self.console_level = level_value(console_level, self.console_level)
            if file_level is not None:
                self.file_level = level_value(file_level, self.file_level)

    def write(self, level: str, message: str, to_console: bool = True) -> None:
        level = (level or "info").lower()
        if level not in LEVELS:
            level = "info"
        value = LEVELS[level]
        ts = time.time()
        clock = time.strftime("%H:%M:%S", time.localtime(ts))
        with self._lock:
            self._buf.append((ts, level, message))
            if to_console and value >= self.console_level:
                line = f"{clock} {level.upper():<5} {message}"
                stream = sys.stderr if level in ("warn", "error") else sys.stdout
                try:
                    print(line, file=stream, flush=True)
                except Exception:  # noqa: BLE001 - 控制台可能已经关了
                    pass
            if self._fh and value >= self.file_level:
                try:
                    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
                    self._fh.write(f"{stamp} {level.upper():<5} {message}\n")
                except (OSError, ValueError):
                    pass

    def lines(self, limit: int = 200) -> list[dict]:
        with self._lock:
            items = list(self._buf)[-max(1, int(limit)):]
        return [{"ts": ts, "level": lv, "text": tx} for ts, lv, tx in items]

    def close(self) -> None:
        with self._lock:
            if self._fh:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None


def describe_exception(exc: BaseException) -> str:
    """把异常翻译成一句人话，方便远程排查。"""
    import socket
    import ssl

    seen = []
    cur: BaseException | None = exc
    depth = 0
    while cur is not None and depth < 5:
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
        depth += 1

    for item in seen:
        if isinstance(item, ssl.SSLCertVerificationError):
            return ("TLS 证书校验失败（可能是这台机器的根证书不全、系统时间不对，"
                    "或者中间有抓包/代理软件在替换证书）")
        if isinstance(item, ssl.SSLError):
            return f"TLS 握手失败（{item}）"
        if isinstance(item, socket.gaierror):
            return "DNS 解析失败（域名解析不出 IP，检查 DNS 或网络）"
        if isinstance(item, TimeoutError):
            return "连接超时"
        if isinstance(item, ConnectionRefusedError):
            return "连接被拒绝"
        if isinstance(item, ConnectionResetError):
            return "连接被重置（常见于被中间设备拦截）"
        if isinstance(item, OSError) and getattr(item, "errno", None) == 10013:
            return "端口被系统占用或禁止（换一个端口）"
    return type(exc).__name__
