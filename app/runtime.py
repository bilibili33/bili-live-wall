"""把配置、状态、API 客户端和两个工作线程串起来。"""
from __future__ import annotations

import os
import uuid

from .bili import BiliClient, sniff_mime
from .config import ConfigStore
from .state import StateStore
from .workers import CaptureWorker, StatusWorker


class Runtime:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.boot_id = uuid.uuid4().hex[:8]
        self.config = ConfigStore(self.root)
        self.state = StateStore(self.config, boot_id=self.boot_id)
        self.bili = BiliClient(self.config.snapshot)
        self.status_worker = StatusWorker(self)
        self.capture_worker = CaptureWorker(self)
        self._fallback_key = None
        self._fallback_blob: tuple[bytes, str] | None = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        os.makedirs(self.config.shot_dir, exist_ok=True)
        self.state.sync_rooms()          # 先按配置建好房间表
        self.state.load()                # 再用上次的缓存填充画面与标题
        for message in self.config.load_warnings:
            self.state.log("warn", message)
        self.state.log("info", f"工作目录：{self.root}")
        self.status_worker.start()
        self.capture_worker.start()

    def stop(self) -> None:
        self.status_worker.stop()
        self.capture_worker.stop()
        self.state.maybe_save(force=True)

    # ------------------------------------------------------------------
    def apply_config(self, patch: dict) -> tuple[dict, list[str]]:
        """保存配置并让工作线程立刻按新配置跑一轮。"""
        previous_port = self.config.snapshot()["server"]["port"]
        cfg = self.config.update(patch)
        self.state.sync_rooms()
        self.state.bump_fallback_rev()
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
