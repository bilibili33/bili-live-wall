"""配置的默认值、归一化与读写。

所有可调项都集中在这里；后台 /admin 会把 SCHEMA 里的每一项都列出来。
"""
from __future__ import annotations

import copy
import json
import os
import threading

from .paths import find_asset

CONFIG_FILE = "config.json"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

DEFAULTS: dict = {
    "version": 1,
    "server": {
        "host": "127.0.0.1",
        "port": 8848,
        "open_browser": True,
    },
    "grid": {
        "cols": 2,
        "rows": 2,
        "gap": 3,
        "radius": 2,
        "background": "#07080a",
        "fit": "cover",
    },
    "status": {
        "enabled": True,
        "interval": 15,
        "timeout": 10,
        "batch_size": 40,
        "stale_after": 60,
        "user_agent": DEFAULT_UA,
        "referer": "https://live.bilibili.com/",
        "cookie": "",
        "api_base": "https://api.live.bilibili.com",
    },
    "capture": {
        "enabled": True,
        "interval": 30,
        "timeout": 15,
        "only_when_live": True,
        "skip_identical": True,
    },
    "ui": {
        "show_title": True,
        "show_anchor": True,
        "show_online": True,
        "show_status_dot": True,
        "show_channel": True,
        "show_timestamp": True,
        "title_lines": 2,
        "title_size": 15,
        "refresh_interval": 3,
        "offline_dim": 0.6,
        "live_accent": "#fb7299",
        "live_accent_width": 2,
        "clock_24h": True,
        "osd_scale": 1.0,
    },
    "assets": {
        "fallback_image": "statics/player-bg.png",
        "digit_dir": "statics",
    },
    "storage": {
        "shot_dir": "data/shots",
        "state_file": "data/state.json",
        "log_lines": 400,
    },
    # 首次运行若没有 config.json，就按这份默认值生成，开箱即用。
    # 每项 uid 是接口用的键，room_id 用于拼直播间链接。
    "rooms": [
        {"uid": 63231, "room_id": 33989, "name": "泛式", "enabled": True},
        {"uid": 730732, "room_id": 42062, "name": "瓶子君152", "enabled": True},
        {"uid": 1351379, "room_id": 17961, "name": "赫萝老师", "enabled": True},
        {"uid": 216025, "room_id": 5252, "name": "尕丶天堂", "enabled": True},
    ],
}

# 归一化规则：类型、取值范围、枚举
SCHEMA: dict[str, dict[str, tuple]] = {
    "server": {
        "host": (str, None, None),
        "port": (int, 1, 65535),
        "open_browser": (bool, None, None),
    },
    "grid": {
        "cols": (int, 1, 12),
        "rows": (int, 1, 12),
        "gap": (int, 0, 64),
        "radius": (int, 0, 64),
        "background": (str, None, None),
        "fit": (str, None, ("cover", "contain")),
    },
    "status": {
        "enabled": (bool, None, None),
        "interval": (int, 5, 3600),
        "timeout": (int, 2, 120),
        "batch_size": (int, 1, 100),
        "stale_after": (int, 10, 86400),
        "user_agent": (str, None, None),
        "referer": (str, None, None),
        "cookie": (str, None, None),
        "api_base": (str, None, None),
    },
    "capture": {
        "enabled": (bool, None, None),
        "interval": (int, 5, 86400),
        "timeout": (int, 2, 120),
        "only_when_live": (bool, None, None),
        "skip_identical": (bool, None, None),
    },
    "ui": {
        "show_title": (bool, None, None),
        "show_anchor": (bool, None, None),
        "show_online": (bool, None, None),
        "show_status_dot": (bool, None, None),
        "show_channel": (bool, None, None),
        "show_timestamp": (bool, None, None),
        "title_lines": (int, 1, 4),
        "title_size": (int, 8, 40),
        "refresh_interval": (int, 1, 120),
        "offline_dim": (float, 0.0, 1.0),
        "live_accent": (str, None, None),
        "live_accent_width": (int, 0, 8),
        "clock_24h": (bool, None, None),
        "osd_scale": (float, 0.5, 3.0),
    },
    "assets": {
        "fallback_image": (str, None, None),
        "digit_dir": (str, None, None),
    },
    "storage": {
        "shot_dir": (str, None, None),
        "state_file": (str, None, None),
        "log_lines": (int, 50, 10000),
    },
}

ROOM_KEYS = ("uid", "room_id", "name", "enabled")


def deep_copy(obj):
    return copy.deepcopy(obj)


def _coerce(value, typ, lo, hi):
    if typ is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
        return bool(value)
    if typ is int:
        try:
            out = int(float(value))
        except (TypeError, ValueError):
            return None
        if lo is not None:
            out = max(lo, out)
        if hi is not None:
            out = min(hi, out)
        return out
    if typ is float:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        if lo is not None:
            out = max(lo, out)
        if hi is not None:
            out = min(hi, out)
        return out
    if typ is str:
        if value is None:
            return ""
        return str(value)
    return value


def normalize_room(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    # B 站 uid 有超过 10^12 的（虚拟主播那批新号能到 3.5e15），范围要给够，
    # 否则超大 uid 会被夹到同一个上限、彼此判成重复房间然后被丢掉。
    uid = _coerce(raw.get("uid"), int, 1, 10**18)
    room_id = _coerce(raw.get("room_id"), int, 0, 10**18)
    if uid is None and room_id is None:
        return None
    name = str(raw.get("name") or "").strip()
    entry = {
        "uid": uid or 0,
        "room_id": room_id or 0,
        "name": name,
        "enabled": bool(raw.get("enabled", True)),
    }
    if raw.get("note"):
        entry["note"] = str(raw["note"])
    return entry


def normalize(raw: dict | None) -> tuple[dict, list[str]]:
    """把任意输入整理成完整、类型正确的配置；返回 (配置, 警告列表)。"""
    warnings: list[str] = []
    raw = raw if isinstance(raw, dict) else {}
    out = deep_copy(DEFAULTS)

    for section, fields in SCHEMA.items():
        src = raw.get(section)
        if not isinstance(src, dict):
            continue
        for key, (typ, lo, hi) in fields.items():
            if key not in src:
                continue
            enum = None
            if isinstance(hi, tuple):
                enum, hi = hi, None
            val = _coerce(src[key], typ, lo, hi)
            if val is None:
                warnings.append(f"{section}.{key} 取值非法，已用默认值 {out[section][key]!r}")
                continue
            if enum is not None and val not in enum:
                warnings.append(f"{section}.{key} 只能是 {enum}，已用默认值 {out[section][key]!r}")
                continue
            if typ in (int, float):
                try:
                    if float(src[key]) != float(val):
                        limit = f"（允许范围 {lo}~{hi}）" if (lo is not None or hi is not None) else ""
                        warnings.append(f"{section}.{key} 超出范围{limit}，已调整为 {val}")
                except (TypeError, ValueError):
                    pass
            out[section][key] = val

    rooms: list[dict] = []
    seen: set[int] = set()
    raw_rooms = raw.get("rooms")
    if not isinstance(raw_rooms, list):
        # 配置文件里没有 rooms 这一项（比如首次运行），就用内置的默认房间
        raw_rooms = deep_copy(DEFAULTS["rooms"])
    for item in raw_rooms:
        room = normalize_room(item)
        if room is None:
            warnings.append("忽略了一条缺少 uid/room_id 的房间记录")
            continue
        key = room["uid"] or -room["room_id"]
        if key in seen:
            warnings.append(f"忽略重复房间 {room['name'] or room['uid']}")
            continue
        seen.add(key)
        rooms.append(room)
    out["rooms"] = rooms
    return out, warnings


class ConfigStore:
    """线程安全的配置容器：进程内共享，改动立即对工作线程生效。"""

    def __init__(self, root: str, filename: str = CONFIG_FILE):
        self.root = os.path.abspath(root)
        self.path = os.path.join(self.root, filename)
        self._lock = threading.RLock()
        self._config: dict = deep_copy(DEFAULTS)
        self._mtime = 0.0
        self.load_warnings: list[str] = []
        self.load_warnings = self.load()

    # -- 路径工具 -----------------------------------------------------
    def resolve(self, relative: str) -> str:
        if not relative:
            return self.root
        if os.path.isabs(relative):
            return os.path.normpath(relative)
        return os.path.normpath(os.path.join(self.root, relative))

    @property
    def shot_dir(self) -> str:
        with self._lock:
            return self.resolve(self._config["storage"]["shot_dir"])

    @property
    def state_file(self) -> str:
        with self._lock:
            return self.resolve(self._config["storage"]["state_file"])

    @property
    def fallback_image(self) -> str:
        with self._lock:
            return find_asset(self._config["assets"]["fallback_image"], self.root)

    @property
    def digit_dir(self) -> str:
        with self._lock:
            return find_asset(self._config["assets"]["digit_dir"], self.root)

    # -- 读写 ---------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return deep_copy(self._config)

    def load(self) -> list[str]:
        warnings: list[str] = []
        raw = None
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                self._mtime = os.path.getmtime(self.path)
            except Exception as exc:  # 配置坏了不能让服务起不来
                warnings.append(f"读取 {CONFIG_FILE} 失败（{exc}），改用默认配置")
                raw = None
        else:
            warnings.append(f"未找到 {CONFIG_FILE}，已按默认值生成")
        config, warns = normalize(raw)
        warnings.extend(warns)
        with self._lock:
            self._config = config
        if raw is None:
            try:
                self.save(config)
            except Exception as exc:
                warnings.append(f"写入 {CONFIG_FILE} 失败：{exc}")
        return warnings

    def save(self, config: dict) -> dict:
        config, warnings = normalize(config)
        with self._lock:
            self._config = config
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(config, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            try:
                self._mtime = os.path.getmtime(self.path)
            except OSError:
                pass
        return config

    def update(self, patch: dict) -> dict:
        """浅层按 section 合并后保存；rooms 直接替换。"""
        current = self.snapshot()
        if isinstance(patch, dict):
            for key, value in patch.items():
                if key == "rooms":
                    current["rooms"] = value
                elif isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key].update(value)
                else:
                    current[key] = value
        return self.save(current)
