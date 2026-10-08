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

    def __init__(self, runtime):
        super().__init__(runtime)
        self._keyframe_seen: set[int] = set()   # 已经提示过"没有关键帧"的房间

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
            if room.keyframe:
                self._keyframe_seen.add(room.uid)
            elif room.is_live() and room.uid not in self._keyframe_seen:
                # 直播中却没有关键帧，这是个重要信号，单独提一次
                self._keyframe_seen.add(room.uid)
                self.rt.state.log("warn", f"「{room.display_name}」在直播，但接口没有返回 keyframe"
                                          f"（接口字段：{sorted(info)[:8]}...）")
            if room.is_live():
                live_count += 1
                self.rt.state.log(
                    "debug",
                    f"  房间 uid={room.uid} {room.display_name} 直播中 "
                    f"人气={room.online} 关键帧={'有' if room.keyframe else '无'} "
                    f"标题={room.title[:30]!r}",
                )
            else:
                self.rt.state.log(
                    "debug",
                    f"  房间 uid={room.uid} {room.display_name} {room.status_text()} "
                    f"关键帧={'有' if room.keyframe else '无'}",
                )
            if room.is_live() and not was_live:
                room.went_live_ts = now
                self.rt.state.log("info", f"「{room.display_name}」开播了：{room.title}")
                if room.keyframe:
                    self.rt.state.log("debug", f"  关键帧地址：{room.keyframe}")
                self.rt.capture_worker.wake()   # 开播立刻抓一张，不等下一个周期
            elif was_live and not room.is_live():
                self.rt.state.log("info", f"「{room.display_name}」已下播")

        for room in rooms:
            if infos.get(room.uid) is None:
                self.rt.state.log("warn", f"  房间 uid={room.uid} {room.display_name} "
                                          f"没有被接口返回：{room.status_error}")

        self._log_problems(errors)
        self.rt.state.log(
            "debug",
            f"状态刷新完成：{len(rooms)} 个房间，{live_count} 个在播，"
            f"用时 {time.time() - started:.2f}s，"
            f"累计请求 {self.rt.bili.requests} 次 / 失败 {self.rt.bili.request_failures} 次",
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
            self.rt.state.log("debug", "截图：当前没有需要抓的房间"
                                       f"（{'只抓直播中的' if only_live else '全部房间'}）")
            return interval

        self.rt.state.log("debug", f"截图开始：{len(candidates)} 个候选房间")
        # 截图线程自己去取一次关键帧地址，不依赖状态线程的结果
        infos, errors = self.rt.bili.fetch_statuses([r.uid for r in candidates])
        self._log_problems([f"截图：{m}" for m in errors])

        shot_dir = self.rt.config.shot_dir
        try:
            os.makedirs(shot_dir, exist_ok=True)
        except OSError as exc:
            self.rt.state.log("error", f"截图目录建不出来：{shot_dir}（{exc}）"
                                       f"—— 如果程序放在只读目录或 U 盘写保护里就会这样")
            return interval

        ok = 0
        same = 0
        for room in candidates:
            info = infos.get(room.uid)
            if info is None:
                self.rt.state.log("warn", f"  截图跳过 uid={room.uid} {room.display_name}：接口未返回该房间")
                self.rt.state.record_shot_error(room.uid, "接口未返回该房间")
                continue
            try:
                status = int(info.get("live_status") or 0)
            except (TypeError, ValueError):
                status = 0
            if only_live and status != 1:
                self.rt.state.log("debug", f"  截图跳过 uid={room.uid} {room.display_name}：已不在播")
                self.rt.state.clear_shot(room.uid)
                continue

            url = (info.get("keyframe") or "").strip()
            if not url and not only_live:
                url = (info.get("cover_from_user") or "").strip()
            if not url:
                self.rt.state.log("warn", f"  截图跳过 uid={room.uid} {room.display_name}："
                                          f"接口没给 keyframe 字段（live_status={status}）")
                self.rt.state.record_shot_error(room.uid, "接口没有给出关键帧")
                continue

            session = int(info.get("live_time") or 0) if status == 1 else 0
            path = os.path.join(shot_dir, f"{room.uid}.jpg")
            self.rt.state.log("debug", f"  截图 uid={room.uid} {room.display_name} ← {url}")
            started = time.time()
            try:
                blob, mime = self.rt.bili.download(url, timeout=timeout)
            except BiliError as exc:
                self.rt.state.log("error", f"  截图失败 uid={room.uid} {room.display_name}：{exc}")
                self.rt.state.record_shot_error(room.uid, f"下载失败：{exc}")
                continue
            if not blob:
                self.rt.state.log("error", f"  截图失败 uid={room.uid} {room.display_name}：下载到空内容")
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
                    self.rt.state.log("error", f"  截图写盘失败 uid={room.uid}：{path}（{exc}）")
                    self.rt.state.record_shot_error(room.uid, f"写盘失败：{exc}")
                    continue
                if session != room.shot_session:
                    self.rt.state.log("info", f"「{room.display_name}」抓到本场第一帧"
                                              f"（{len(blob) // 1024} KB，{mime}）")
                else:
                    self.rt.state.log("debug", f"  画面已更新（{len(blob) // 1024} KB，{mime}，"
                                              f"{time.time() - started:.2f}s）")
            else:
                same += 1
                self.rt.state.log("debug", f"  画面没变，跳过写盘（{len(blob) // 1024} KB，"
                                          f"{time.time() - started:.2f}s）")
            self.rt.state.record_shot(room.uid, blob, mime, session, changed)
            ok += 1

        self.rt.state.log("debug", f"截图完成：{ok}/{len(candidates)} 个房间取到画面，"
                                   f"其中 {same} 个内容未变；累计请求 {self.rt.bili.requests} 次 / "
                                   f"失败 {self.rt.bili.request_failures} 次")
        self.rt.state.maybe_save()
        return interval
