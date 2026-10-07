"""两个互相独立的后台线程：状态监控 与 截图抓取。"""
from __future__ import annotations

import os
import threading
import time

from .bili import BiliError

IDLE_INTERVAL = 20.0


class _Worker(threading.Thread):
    label = "worker"

    def __init__(self, runtime):
        super().__init__(name=f"bili-{self.label}", daemon=True)
        self.rt = runtime
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_problems: tuple = ()

    def _log_problems(self, problems: list[str]) -> None:
        """同一批错误只记一次，不然一个坏房间会每轮刷屏。"""
        key = tuple(sorted(set(problems)))
        if key == self._last_problems:
            return
        self._last_problems = key
        for message in key:
            self.rt.state.log("warn", message)

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _sleep(self, seconds: float) -> bool:
        """可被打断的等待；返回 True 表示收到停止信号。"""
        self._wake.wait(timeout=max(0.2, seconds))
        self._wake.clear()
        return self._stop.is_set()

    def run(self) -> None:  # pragma: no cover - 线程体
        self.rt.state.log("info", f"{self.label} 线程已启动")
        while not self._stop.is_set():
            try:
                interval = self.tick()
            except Exception as exc:  # 任何异常都不能让线程死掉
                self.rt.state.log("error", f"{self.label} 线程异常：{exc!r}")
                interval = IDLE_INTERVAL
            if self._sleep(interval):
                break
        self.rt.state.log("info", f"{self.label} 线程已停止")

    def tick(self) -> float:
        raise NotImplementedError


class StatusWorker(_Worker):
    """只负责“在不在播 / 播什么”，一次请求覆盖所有房间。"""

    label = "status"

    def tick(self) -> float:
        cfg = self.rt.config.snapshot()
        st = cfg["status"]
        if not st.get("enabled", True):
            return IDLE_INTERVAL

        interval = float(st.get("interval") or 15)
        rooms = self.rt.state.rooms(only_enabled=True)
        if not rooms:
            return interval

        started = time.time()
        infos, errors = self.rt.bili.fetch_statuses([r.uid for r in rooms])
        now = time.time()

        live_count = 0
        for room in rooms:
            info = infos.get(room.uid)
            if info is None:
                room.status_error = errors[0] if errors else "接口未返回该房间"
                continue
            was_live = room.live_status == 1
            room.uname = info.get("uname") or room.uname
            room.title = info.get("title") or ""
            room.area = info.get("area_v2_name") or info.get("area_name") or ""
            try:
                room.online = int(info.get("online") or 0)
            except (TypeError, ValueError):
                room.online = 0
            try:
                room.live_status = int(info.get("live_status") or 0)
            except (TypeError, ValueError):
                room.live_status = 0
            try:
                room.live_time = int(info.get("live_time") or 0)
            except (TypeError, ValueError):
                room.live_time = 0
            room.cover = info.get("cover_from_user") or info.get("cover") or room.cover
            room.keyframe = info.get("keyframe") or ""
            if not room.room_id:
                try:
                    room.room_id = int(info.get("room_id") or 0)
                except (TypeError, ValueError):
                    pass
            room.status_ts = now          # 本次尝试时间
            room.status_ok_ts = now       # 本次成功时间
            room.status_error = ""
            if room.is_live():
                live_count += 1
            if room.is_live() and not was_live:
                room.went_live_ts = now
                self.rt.state.log("info", f"「{room.display_name}」开播了：{room.title}")
                self.rt.capture_worker.wake()   # 开播立刻抓一张，不等下一个周期
            elif was_live and not room.is_live():
                self.rt.state.log("info", f"「{room.display_name}」已下播")

        self._log_problems(errors)
        self.rt.state.log(
            "debug",
            f"状态刷新完成：{len(rooms)} 个房间，{live_count} 个在播，用时 {time.time() - started:.2f}s",
        )
        self.rt.state.maybe_save()
        return interval


class CaptureWorker(_Worker):
    """只负责截图，按自己的节奏跑，比状态监控慢很多也没关系。"""

    label = "capture"

    def tick(self) -> float:
        cfg = self.rt.config.snapshot()
        cap = cfg["capture"]
        if not cap.get("enabled", True):
            return IDLE_INTERVAL

        interval = float(cap.get("interval") or 30)
        only_live = bool(cap.get("only_when_live", True))
        skip_identical = bool(cap.get("skip_identical", True))
        timeout = float(cap.get("timeout") or 15)

        candidates = [
            r for r in self.rt.state.rooms(only_enabled=True)
            if r.live_status == 1 or not only_live
        ]
        if not candidates:
            return interval

        # 截图线程自己去取一次关键帧地址，不依赖状态线程的结果
        infos, errors = self.rt.bili.fetch_statuses([r.uid for r in candidates])
        self._log_problems([f"截图：{m}" for m in errors])

        shot_dir = self.rt.config.shot_dir
        os.makedirs(shot_dir, exist_ok=True)

        ok = 0
        for room in candidates:
            info = infos.get(room.uid)
            if info is None:
                self.rt.state.record_shot_error(room.uid, "接口未返回该房间")
                continue
            try:
                status = int(info.get("live_status") or 0)
            except (TypeError, ValueError):
                status = 0
            if only_live and status != 1:
                self.rt.state.clear_shot(room.uid)
                continue

            url = (info.get("keyframe") or "").strip()
            if not url and not only_live:
                url = (info.get("cover_from_user") or "").strip()
            if not url:
                self.rt.state.record_shot_error(room.uid, "接口没有给出关键帧")
                continue

            session = int(info.get("live_time") or 0) if status == 1 else 0
            path = os.path.join(shot_dir, f"{room.uid}.jpg")
            try:
                blob, mime = self.rt.bili.download(url, timeout=timeout)
            except BiliError as exc:
                self.rt.state.record_shot_error(room.uid, f"下载失败：{exc}")
                continue
            if not blob:
                self.rt.state.record_shot_error(room.uid, "下载到空文件")
                continue

            changed = True
            if skip_identical:
                try:
                    with open(path, "rb") as fh:
                        changed = fh.read() != blob
                except OSError:
                    changed = True
            if changed:
                try:
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as fh:
                        fh.write(blob)
                    os.replace(tmp, path)
                except OSError as exc:
                    self.rt.state.record_shot_error(room.uid, f"写盘失败：{exc}")
                    continue
                if session != room.shot_session:
                    self.rt.state.log("info", f"「{room.display_name}」抓到本场第一帧（{len(blob) // 1024} KB）")
            self.rt.state.record_shot(room.uid, blob, mime, session, changed)
            ok += 1

        self.rt.state.log("debug", f"截图完成：{ok}/{len(candidates)} 个房间")
        self.rt.state.maybe_save()
        return interval
