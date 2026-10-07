"""进程内共享状态：房间的直播状态、截图信息与运行日志。"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

STATUS_TEXT = {
    0: "未开播",
    1: "直播中",
    2: "轮播",
}


def _now() -> float:
    return time.time()


class RoomState:
    __slots__ = (
        "uid", "room_id", "name", "enabled",
        "uname", "title", "area", "online", "live_status", "live_time",
        "cover", "keyframe", "status_ts", "status_error", "status_ok_ts",
        "shot_ts", "shot_session", "shot_error", "shot_size", "shot_rev",
        "shot_captured_ts", "shot_mime", "captures", "failures", "went_live_ts",
    )

    def __init__(self, uid: int, room_id: int = 0, name: str = "", enabled: bool = True):
        self.uid = int(uid)
        self.room_id = int(room_id or 0)
        self.name = name or ""
        self.enabled = bool(enabled)
        self.uname = ""
        self.title = ""
        self.area = ""
        self.online = 0
        self.live_status = 0
        self.live_time = 0
        self.cover = ""
        self.keyframe = ""
        self.status_ts = 0.0          # 最后一次状态刷新的时间
        self.status_ok_ts = 0.0       # 最后一次成功刷新的时间
        self.status_error = ""
        self.shot_ts = 0.0            # 当前画面内容的产生时间
        self.shot_session = None      # 画面属于哪一场直播（live_time）
        self.shot_error = ""
        self.shot_size = 0
        self.shot_rev = 0             # 画面内容版本号，用于前端去重
        self.shot_captured_ts = 0.0   # 最后一次抓取动作的时间
        self.shot_mime = "image/jpeg"
        self.captures = 0
        self.failures = 0
        self.went_live_ts = 0.0

    @property
    def key(self) -> int:
        return self.uid or -self.room_id

    @property
    def display_name(self) -> str:
        return self.name or self.uname or (f"房间 {self.room_id}" if self.room_id else f"uid {self.uid}")

    def is_live(self) -> bool:
        return self.live_status == 1


class StateStore:
    def __init__(self, config_store, boot_id: str = ""):
        self._cfg = config_store
        self.boot_id = boot_id      # 每次启动都不同，用来让浏览器丢弃上次进程留下的图片缓存
        self._lock = threading.RLock()
        self._rooms: dict[int, RoomState] = {}
        self._order: list[int] = []
        self._log: deque[tuple[float, str, str]] = deque(maxlen=1000)
        self._fallback_rev = 0
        self._fallback_mime = "image/png"
        self._dirty = False
        self._last_save = 0.0

    # -- 日志 ---------------------------------------------------------
    def log(self, level: str, message: str) -> None:
        with self._lock:
            self._log.append((_now(), level, message))
        if level in ("error", "warn"):
            print(f"[{level}] {message}", flush=True)

    def logs(self, limit: int = 200) -> list[dict]:
        with self._lock:
            items = list(self._log)[-limit:]
        return [{"ts": ts, "level": lv, "text": tx} for ts, lv, tx in items]

    # -- 房间同步 -----------------------------------------------------
    def sync_rooms(self) -> None:
        """让状态表跟随配置里的房间列表。"""
        cfg = self._cfg.snapshot()
        with self._lock:
            wanted: list[int] = []
            for item in cfg.get("rooms", []):
                uid = int(item.get("uid") or 0)
                if not uid:
                    continue
                room_id = int(item.get("room_id") or 0)
                existing = self._rooms.get(uid)
                if existing is None:
                    existing = RoomState(uid, room_id, item.get("name") or "", item.get("enabled", True))
                    self._rooms[uid] = existing
                else:
                    existing.room_id = room_id or existing.room_id
                    existing.name = item.get("name") or existing.name
                    existing.enabled = bool(item.get("enabled", True))
                wanted.append(uid)
            for uid in list(self._rooms):
                if uid not in wanted:
                    del self._rooms[uid]
            self._order = wanted
        self._mark_dirty()

    def rooms(self, only_enabled: bool = False) -> list[RoomState]:
        with self._lock:
            out = [self._rooms[u] for u in self._order if u in self._rooms]
        if only_enabled:
            out = [r for r in out if r.enabled]
        return out

    def get(self, uid: int) -> RoomState | None:
        with self._lock:
            return self._rooms.get(int(uid))

    def set_enabled(self, uid: int, enabled: bool) -> bool:
        with self._lock:
            room = self._rooms.get(int(uid))
            if not room:
                return False
            room.enabled = bool(enabled)
        self._mark_dirty()
        return True

    # -- 截图 ---------------------------------------------------------
    def bump_fallback_rev(self) -> None:
        with self._lock:
            self._fallback_rev += 1

    def fallback_rev(self) -> int:
        with self._lock:
            return self._fallback_rev

    def record_shot(self, uid: int, blob: bytes, mime: str, session, changed: bool) -> None:
        now = _now()
        with self._lock:
            room = self._rooms.get(int(uid))
            if not room:
                return
            room.shot_captured_ts = now
            room.shot_error = ""
            room.failures = 0
            room.captures += 1
            room.shot_mime = mime or "image/jpeg"
            if changed:
                room.shot_ts = now
                room.shot_rev += 1
                room.shot_size = len(blob)
            elif not room.shot_ts:
                room.shot_ts = now
            room.shot_session = session
        self._mark_dirty()

    def record_shot_error(self, uid: int, message: str) -> None:
        with self._lock:
            room = self._rooms.get(int(uid))
            if not room:
                return
            room.shot_captured_ts = _now()
            room.shot_error = message
            room.failures += 1
        self._mark_dirty()

    def clear_shot(self, uid: int) -> None:
        with self._lock:
            room = self._rooms.get(int(uid))
            if room and room.shot_rev:
                room.shot_ts = 0.0
                room.shot_session = None
                room.shot_rev += 1
                self._dirty = True

    # -- 对外快照 -----------------------------------------------------
    def snapshot(self) -> dict:
        cfg = self._cfg.snapshot()
        only_live = bool(cfg["capture"].get("only_when_live", True))
        now = _now()
        stale_after = float(cfg["status"].get("stale_after") or 60)
        with self._lock:
            fallback_rev = self._fallback_rev
            rooms = []
            for uid in self._order:
                room = self._rooms.get(uid)
                if room is None:
                    continue
                live = room.live_status == 1
                has_shot = room.shot_rev > 0 and os.path.isfile(
                    os.path.join(self._cfg.shot_dir, f"{room.uid}.jpg")
                )
                fresh = has_shot and (
                    (live and room.shot_session == room.live_time)
                    or ((not live) and (not only_live) and room.shot_session in (0, None))
                )
                display = "shot" if fresh else "fallback"
                if display == "shot":
                    image = f"/shots/{room.uid}.jpg?r={room.shot_rev}&b={self.boot_id}"
                    image_ts = room.shot_ts
                else:
                    image = f"/assets/fallback.png?r={fallback_rev}"
                    image_ts = 0.0
                rooms.append({
                    "uid": room.uid,
                    "room_id": room.room_id,
                    "name": room.display_name,
                    "anchor": room.uname,
                    "title": room.title,
                    "area": room.area,
                    "online": room.online,
                    "live": live,
                    "live_status": room.live_status,
                    "live_text": STATUS_TEXT.get(room.live_status, "未知"),
                    "enabled": room.enabled,
                    "display": display,
                    "image": image,
                    "image_ts": image_ts,
                    "shot_rev": room.shot_rev,
                    "status_age": round(now - room.status_ok_ts, 1) if room.status_ok_ts else None,
                    "stale": bool(room.status_ok_ts and (now - room.status_ok_ts) > stale_after),
                    "status_error": room.status_error,
                    "shot_error": room.shot_error,
                    "shot_size": room.shot_size,
                    "captures": room.captures,
                    "failures": room.failures,
                    "live_time": room.live_time,
                    "url": f"https://live.bilibili.com/{room.room_id}" if room.room_id else "",
                })
        return {
            "server_time": now,
            "grid": cfg["grid"],
            "ui": cfg["ui"],
            "status_interval": cfg["status"]["interval"],
            "capture_interval": cfg["capture"]["interval"],
            "rooms": rooms,
            "cells": cfg["grid"]["cols"] * cfg["grid"]["rows"],
        }

    # -- 持久化 -------------------------------------------------------
    def _mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    def maybe_save(self, force: bool = False) -> None:
        with self._lock:
            if not self._dirty:
                return
            if not force and (_now() - self._last_save) < 15:
                return
            self._dirty = False
            self._last_save = _now()
            rooms = [self._rooms[u] for u in self._order if u in self._rooms]
        path = self._cfg.state_file
        payload = {
            "saved_at": _now(),
            "rooms": [
                {
                    "uid": r.uid, "room_id": r.room_id, "name": r.name, "enabled": r.enabled,
                    "uname": r.uname, "title": r.title, "area": r.area, "online": r.online,
                    "live_status": r.live_status, "live_time": r.live_time,
                    "cover": r.cover, "keyframe": r.keyframe,
                    "status_ts": r.status_ts, "shot_ts": r.shot_ts, "shot_session": r.shot_session,
                    "shot_rev": r.shot_rev, "shot_size": r.shot_size, "shot_mime": r.shot_mime,
                    "captures": r.captures, "failures": r.failures,
                }
                for r in rooms
            ],
        }
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            self.log("warn", f"状态落盘失败：{exc}")

    def load(self) -> None:
        path = self._cfg.state_file
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            self.log("warn", f"读取状态缓存失败：{exc}")
            return
        saved = payload.get("rooms") or []
        with self._lock:
            for item in saved:
                try:
                    uid = int(item.get("uid") or 0)
                except (TypeError, ValueError):
                    continue
                room = self._rooms.get(uid)
                if room is None:
                    continue
                for field in ("uname", "title", "area", "cover", "keyframe", "shot_error", "status_error"):
                    if field in item:
                        setattr(room, field, item[field] or "")
                for field in ("online", "live_status", "live_time", "shot_rev", "shot_size", "captures", "failures"):
                    try:
                        setattr(room, field, int(item.get(field) or 0))
                    except (TypeError, ValueError):
                        pass
                for field in ("status_ts", "shot_ts"):
                    try:
                        setattr(room, field, float(item.get(field) or 0))
                    except (TypeError, ValueError):
                        pass
                room.shot_session = item.get("shot_session")
                if item.get("shot_mime"):
                    room.shot_mime = item["shot_mime"]
        self.log("info", f"已载入上次状态缓存（{len(saved)} 个房间）")
