"""把配置、日志、状态、API 客户端和两个工作线程串起来。"""
from __future__ import annotations

import os
import platform
import ssl
import sys
import urllib.request
import uuid

from .bili import BiliClient, sniff_mime
from .config import ConfigStore
from .logging_ import LogSink
from .paths import bundle_dir, is_frozen
from .state import StateStore
from . import tls
from .workers import CaptureWorker, StatusWorker


class Runtime:
    def __init__(self, root: str, console_level: str | None = None):
        self.root = os.path.abspath(root)
        self.boot_id = uuid.uuid4().hex[:8]
        self.config = ConfigStore(self.root)
        cfg = self.config.snapshot()

        level = console_level or cfg["server"].get("log_level") or "debug"
        log_path = self.config.log_file if cfg["server"].get("log_to_file", True) else None
        self.sink = LogSink(log_path, console_level=level, file_level="debug")

        self.state = StateStore(self.config, boot_id=self.boot_id, sink=self.sink)
        self.bili = BiliClient(self.config.snapshot, state=self.state, root=self.root)
        self.status_worker = StatusWorker(self)
        self.capture_worker = CaptureWorker(self)
        self._fallback_key = None
        self._fallback_blob: tuple[bytes, str] | None = None

    # ------------------------------------------------------------------
    def start(self, with_workers: bool = True, log_env: bool = True) -> None:
        os.makedirs(self.config.shot_dir, exist_ok=True)
        self.state.sync_rooms()          # 先按配置建好房间表
        self.state.load()                # 再用上次的缓存填充画面与标题
        for message in self.config.load_warnings:
            self.state.log("warn", message)
        if log_env:
            self.log_environment()
        if with_workers:
            self.status_worker.start()
            self.capture_worker.start()

    def stop(self) -> None:
        self.status_worker.stop()
        self.capture_worker.stop()
        self.state.maybe_save(force=True)
        self.sink.close()

    # ------------------------------------------------------------------
    def environment_lines(self) -> list[str]:
        """把环境信息整理成一段文本——远程排查全靠这一段。"""
        cfg = self.config.snapshot()
        lines: list[str] = []
        add = lines.append
        add("=" * 62)
        add(f"程序版本   : 1.1.0    打包运行: {'是' if is_frozen() else '否（源码）'}")
        add(f"Python     : {sys.version.split()[0]} ({platform.machine()})")
        add(f"操作系统   : {platform.platform()}")
        add(f"工作目录   : {self.root}")
        add(f"程序目录   : {bundle_dir()}")
        add(f"配置文件   : {self.config.path}")
        add(f"截图目录   : {self.config.shot_dir}")
        add(f"日志文件   : {self.sink.path or '（未启用）'}"
            + (f"  写入失败：{self.sink.file_error}" if self.sink.file_error else ""))
        add(f"未开播底图 : {self.config.fallback_image}")
        digits = sorted(self.digits().keys())
        add(f"数字素材   : {'0-9 齐全' if len(digits) >= 10 else '缺 ' + str(10 - len(digits)) + ' 个'}"
            f"   目录 {self.config.digit_dir}")
        add(f"房间数     : {len(cfg['rooms'])} 个"
            f"   状态间隔 {cfg['status']['interval']}s   截图间隔 {cfg['capture']['interval']}s")
        add(f"日志级别   : 控制台 {cfg['server'].get('log_level')}")
        add(f"OpenSSL    : {ssl.OPENSSL_VERSION}")
        try:
            stats = ssl.create_default_context().cert_store_stats()
            add(f"系统根证书 : 系统库 x509={stats.get('x509')} crl={stats.get('crl')}"
                f"（TLS 校验失败通常是这里不全）")
        except Exception as exc:  # noqa: BLE001
            add(f"系统根证书 : 读取失败 {exc}")
        try:
            report = tls.ca_report(self.root)
            if report["bundled_loaded"]:
                add(f"内置 CA 包 : 已加载 {report['bundled_certs']} 个根证书  {report['ca_bundle']}")
            else:
                add(f"内置 CA 包 : 没加载成功（找的是 {report['searched']}）"
                    f"—— 目标机器根证书库偏旧时会更容易出 TLS 问题")
        except Exception as exc:  # noqa: BLE001
            add(f"内置 CA 包 : 加载失败 {exc}")
        try:
            proxies = urllib.request.getproxies()
        except Exception:  # noqa: BLE001
            proxies = {}
        add(f"系统代理   : {proxies or '未设置'}"
            + ("   ← 如果代理没开或不通，所有请求都会失败" if proxies else ""))
        add("=" * 62)
        return lines

    def log_environment(self) -> None:
        for line in self.environment_lines():
            self.state.log("info", line)

    # ------------------------------------------------------------------
    def apply_config(self, patch: dict) -> tuple[dict, list[str]]:
        """保存配置并让工作线程立刻按新配置跑一轮。"""
        previous_port = self.config.snapshot()["server"]["port"]
        cfg = self.config.update(patch)
        self.state.sync_rooms()
        self.state.bump_fallback_rev()
        self.sink.set_levels(console_level=cfg["server"].get("log_level"))
        self.wake()
        warnings: list[str] = []
        if cfg["server"]["port"] != previous_port:
            warnings.append("端口已改动，需要重启服务才会生效")
        return cfg, warnings

    def wake(self) -> None:
        self.status_worker.wake()
        self.capture_worker.wake()

    # ------------------------------------------------------------------
    def fallback_image(self) -> tuple[bytes, str]:
        path = self.config.fallback_image
        try:
            stat = os.stat(path)
            key = (path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._fallback_key = None
            self._fallback_blob = None
            return b"", "image/png"
        if key != self._fallback_key or self._fallback_blob is None:
            try:
                with open(path, "rb") as fh:
                    blob = fh.read()
            except OSError:
                return b"", "image/png"
            self._fallback_key = key
            self._fallback_blob = (blob, sniff_mime(blob))
        return self._fallback_blob

    def shot_path(self, uid: int) -> str:
        return os.path.join(self.config.shot_dir, f"{int(uid)}.jpg")

    def digits(self) -> dict[str, str]:
        """返回 0-9 数字图片在磁盘上的路径。"""
        base = self.config.digit_dir
        found: dict[str, str] = {}
        for digit in range(10):
            for ext in (".png", ".webp", ".jpg", ".gif"):
                path = os.path.join(base, f"{digit}{ext}")
                if os.path.isfile(path):
                    found[str(digit)] = path
                    break
        return found
