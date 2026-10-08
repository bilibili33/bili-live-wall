"""B 站直播间 API 客户端（只依赖标准库）。

接口：
  * get_status_info_by_uids —— 一次请求拿回所有房间的 直播状态/标题/人气/主播名/封面/keyframe
  * room_init              —— 把「房间号 / 短号 / 直播间链接」解析成 room_id + uid

关键帧图挂在 i0.hdslb.com 这类 CDN 上。不同网络环境下个别 CDN 域名可能拉不通，
所以下载失败会自动换 i1/i2 再试，并且每一次尝试都会写日志。
"""
from __future__ import annotations

import gzip
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .logging_ import describe_exception

LIVE_STATUS = {
    0: "offline",   # 未开播
    1: "live",      # 直播中
    2: "round",     # 轮播（放录像）
}

IMAGE_HOST_RE = re.compile(r"^i(\d)\.hdslb\.com$", re.I)
IMAGE_HOSTS = ("i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com")


class BiliError(RuntimeError):
    pass


def _decompress(raw: bytes, encoding: str) -> bytes:
    if encoding == "gzip":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    return raw


def host_variants(url: str) -> list[str]:
    """把 i0.hdslb.com 换成 i1/i2，用来在某个 CDN 域名不通时兜底。"""
    parts = urllib.parse.urlsplit(url)
    match = IMAGE_HOST_RE.match(parts.netloc)
    if not match:
        return [url]
    out = [url]
    for host in IMAGE_HOSTS:
        if host.lower() != parts.netloc.lower():
            out.append(urllib.parse.urlunsplit(
                (parts.scheme or "https", host, parts.path, parts.query, parts.fragment)))
    return out


def _short_url(url: str, limit: int = 110) -> str:
    if len(url) <= limit:
        return url
    return url[: limit - 3] + "..."


class BiliClient:
    def __init__(self, config_provider, state=None, root: str | None = None):
        # config_provider 返回一份配置快照（dict）
        self._cfg = config_provider
        self.state = state          # 可选：用来写日志
        self.root = root            # 可选：用来定位内置 CA 包
        self.requests = 0
        self.request_failures = 0

    # ------------------------------------------------------------------
    def log(self, level: str, message: str) -> None:
        if self.state is not None:
            self.state.log(level, message)

    def _opts(self):
        cfg = self._cfg()
        st = cfg.get("status", {})
        return {
            "base": (st.get("api_base") or "https://api.live.bilibili.com").rstrip("/"),
            "timeout": float(st.get("timeout") or 10),
            "ua": st.get("user_agent") or "Mozilla/5.0",
            "referer": st.get("referer") or "https://live.bilibili.com/",
            "cookie": (st.get("cookie") or "").strip(),
        }

    def _json_headers(self) -> dict:
        o = self._opts()
        headers = {
            "User-Agent": o["ua"],
            "Referer": o["referer"],
            "Origin": "https://live.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip",
        }
        if o["cookie"]:
            headers["Cookie"] = o["cookie"]
        return headers

    def _image_headers(self, with_referer: bool = True) -> dict:
        """图片请求单独一套头：不带 Origin，Accept 也要像浏览器。"""
        o = self._opts()
        headers = {
            "User-Agent": o["ua"],
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip",
        }
        if with_referer:
            headers["Referer"] = o["referer"]
        if o["cookie"]:
            headers["Cookie"] = o["cookie"]
        return headers

    # ------------------------------------------------------------------
    def _open(self, url: str, timeout: float, headers: dict, what: str):
        """发一个请求，把结果和耗时都记下来。返回 (bytes, 信息字典)。"""
        o = self._opts()
        req = urllib.request.Request(url, headers=headers)
        started = time.time()
        self.requests += 1
        try:
            if self.root:
                from . import tls
                response = tls.opener(self.root).open(req, timeout=timeout or o["timeout"])
            else:
                response = urllib.request.urlopen(req, timeout=timeout or o["timeout"])
            with response as resp:
                raw = resp.read()
                elapsed = time.time() - started
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
                blob = _decompress(raw, encoding)
                info = {
                    "status": resp.status,
                    "content_type": resp.headers.get("Content-Type") or "",
                    "encoding": encoding,
                    "bytes": len(blob),
                    "elapsed": elapsed,
                    "final_url": resp.geturl(),
                }
                self.log("debug", f"[{what}] GET {_short_url(url)} → {resp.status} "
                                  f"{len(blob)}B {info['content_type'] or '?'} {elapsed:.2f}s")
                return blob, info
        except urllib.error.HTTPError as exc:
            elapsed = time.time() - started
            self.request_failures += 1
            body = b""
            try:
                body = exc.read()[:400]
            except Exception:  # noqa: BLE001
                pass
            detail = body.decode("utf-8", "replace").strip().replace("\n", " ")[:200]
            self.log("error", f"[{what}] GET {_short_url(url)} 失败：HTTP {exc.code} {exc.reason}"
                              f"（{elapsed:.2f}s）")
            if detail:
                self.log("error", f"[{what}]   响应内容：{detail}")
            if exc.code in (403, 401):
                self.log("error", f"[{what}]   403/401 一般是防盗链或风控，"
                                  f"Referer={headers.get('Referer')!r} 可能不被这个 CDN 接受")
            raise BiliError(f"HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            self.request_failures += 1
            reason = getattr(exc, "reason", exc)
            self.log("error", f"[{what}] GET {_short_url(url)} 失败：{describe_exception(reason)}"
                              f"（{time.time() - started:.2f}s）")
            self.log("error", f"[{what}]   底层错误：{type(reason).__name__}: {reason}")
            raise BiliError(f"{describe_exception(reason)}") from exc
        except (TimeoutError, socket.timeout) as exc:
            self.request_failures += 1
            self.log("error", f"[{what}] GET {_short_url(url)} 超时（{time.time() - started:.2f}s）")
            raise BiliError("请求超时") from exc
        except (ssl.SSLError, OSError) as exc:
            self.request_failures += 1
            self.log("error", f"[{what}] GET {_short_url(url)} 失败：{describe_exception(exc)}")
            self.log("error", f"[{what}]   底层错误：{type(exc).__name__}: {exc}")
            raise BiliError(describe_exception(exc)) from exc

    def _get_json(self, path: str, params: list[tuple[str, str]] | None = None,
                  timeout: float | None = None) -> dict:
        o = self._opts()
        url = o["base"] + path
        if params:
            # 手工拼接，保留 uids[0] 里的方括号
            query = "&".join(
                f"{urllib.parse.quote(str(k), safe='[]')}={urllib.parse.quote(str(v))}"
                for k, v in params
            )
            url = f"{url}?{query}"
        blob, _info = self._open(url, timeout or o["timeout"], self._json_headers(), "api")
        try:
            data = json.loads(blob.decode("utf-8", "replace"))
        except ValueError as exc:
            self.log("error", f"[api] {_short_url(url)} 返回的不是 JSON（{exc}）"
                              f"，前 200 字节：{blob[:200]!r}")
            raise BiliError(f"接口返回的不是 JSON（{exc}）") from exc
        if not isinstance(data, dict):
            raise BiliError("接口返回格式异常")
        return data

    # ------------------------------------------------------------------
    def fetch_statuses(self, uids: list[int]) -> tuple[dict[int, dict], list[str]]:
        """按 uid 批量取直播状态。返回 ({uid: info}, 错误信息列表)。"""
        result: dict[int, dict] = {}
        errors: list[str] = []
        if not uids:
            return result, errors

        cfg = self._cfg()
        batch = max(1, int(cfg.get("status", {}).get("batch_size") or 40))
        chunks = [uids[i:i + batch] for i in range(0, len(uids), batch)]

        for chunk in chunks:
            params = [(f"uids[{i}]", str(u)) for i, u in enumerate(chunk)]
            try:
                payload = self._get_json("/room/v1/Room/get_status_info_by_uids", params)
            except BiliError as exc:
                errors.append(f"状态接口失败：{exc}")
                continue
            code = payload.get("code")
            if code != 0:
                errors.append(f"状态接口返回 code={code} {payload.get('message') or ''}".strip())
                continue
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                errors.append("状态接口 data 字段异常")
                continue
            for key, item in data.items():
                if not isinstance(item, dict):
                    continue
                try:
                    uid = int(item.get("uid") or key)
                except (TypeError, ValueError):
                    continue
                result[uid] = item
            missing = [u for u in chunk if u not in result]
            if missing:
                errors.append(f"{len(missing)} 个房间没有被返回（可能已注销/被隐藏）")
        return result, errors

    def room_init(self, ident: str) -> dict:
        payload = self._get_json("/room/v1/Room/room_init", [("id", str(ident))])
        if payload.get("code") != 0:
            raise BiliError(f"room_init 失败：code={payload.get('code')} "
                            f"{payload.get('message') or ''}".strip())
        data = payload.get("data") or {}
        if not data.get("room_id"):
            raise BiliError("room_init 未返回 room_id")
        return data

    def get_info(self, room_id: int) -> dict:
        payload = self._get_json("/room/v1/Room/get_info", [("room_id", str(room_id))])
        if payload.get("code") != 0:
            raise BiliError(f"get_info 失败：code={payload.get('code')} "
                            f"{payload.get('message') or ''}".strip())
        return payload.get("data") or {}

    # ------------------------------------------------------------------
    def probe(self, url: str, timeout: float = 15, with_referer: bool = True,
              what: str = "探测"):
        """给自检用的裸请求：返回 (字节, 信息)，失败直接抛异常。"""
        return self._open(url, timeout, self._image_headers(with_referer), what)

    def download(self, url: str, timeout: float | None = None) -> tuple[bytes, str]:
        """下载图片。返回 (字节, 实际内容类型)。

        会依次尝试 i0/i1/i2 三个 CDN 域名；遇到 403 还会去掉 Referer 再试一次
        （有些 CDN 边缘节点对防盗链的判断不一致）。
        """
        o = self._opts()
        timeout = timeout or o["timeout"]
        attempts: list[tuple[str, bool]] = []
        for host_url in host_variants(url):
            attempts.append((host_url, True))
            attempts.append((host_url, False))   # 不带 Referer 再来一次

        problems: list[str] = []
        tried: set[tuple[str, bool]] = set()
        for target, with_referer in attempts:
            if (target, with_referer) in tried:
                continue
            tried.add((target, with_referer))
            tag = "图片" if with_referer else "图片(无Referer)"
            try:
                blob, info = self._open(target, timeout, self._image_headers(with_referer), tag)
            except BiliError as exc:
                problems.append(f"{urllib.parse.urlsplit(target).netloc}"
                                f"{'' if with_referer else '(无Referer)'}: {exc}")
                continue
            if not blob:
                problems.append(f"{urllib.parse.urlsplit(target).netloc}: 内容为空")
                continue
            return blob, sniff_mime(blob)

        summary = "；".join(dict.fromkeys(problems)) or "所有 CDN 都没取到内容"
        self.log("error", f"[图片] 全部尝试失败：{summary}")
        raise BiliError(summary)


# ----------------------------------------------------------------------
def sniff_mime(blob: bytes) -> str:
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob[:2] == b"BM":
        return "image/bmp"
    if blob[:4] == b"<svg" or blob[:5] == b"<?xml":
        return "image/svg+xml"
    return "application/octet-stream"


def parse_room_input(text: str) -> dict:
    """把用户粘贴的内容解析成候选标识。

    支持：
      https://live.bilibili.com/21452505
      https://live.bilibili.com/blanc/21452505
      https://space.bilibili.com/434334701
      21452505 / 434334701
    """
    text = (text or "").strip()
    if not text:
        raise BiliError("内容为空")

    m = re.search(r"live\.bilibili\.com/(?:blanc/|blackboard/)?(\d+)", text)
    if m:
        return {"kind": "room", "value": m.group(1)}

    m = re.search(r"space\.bilibili\.com/(\d+)", text)
    if m:
        return {"kind": "uid", "value": m.group(1)}

    # 纯数字：房间号或 uid，B 站 uid 最长能到 16 位以上，别卡太死
    m = re.fullmatch(r"\s*(\d{1,19})\s*", text)
    if m:
        return {"kind": "number", "value": m.group(1)}

    raise BiliError("无法识别，请粘贴直播间链接、空间链接或纯数字房间号")


def resolve_room(client: BiliClient, text: str) -> dict:
    """把输入解析成 {uid, room_id, uname, title, live_status}。"""
    parsed = parse_room_input(text)

    if parsed["kind"] == "uid":
        uid = int(parsed["value"])
        infos, errors = client.fetch_statuses([uid])
        info = infos.get(uid)
        if not info:
            raise BiliError(f"uid {uid} 查询不到直播间" + (f"（{errors[0]}）" if errors else ""))
        return _room_from_info(info)

    ident = parsed["value"]
    try:
        init = client.room_init(ident)
    except BiliError:
        init = None

    if init:
        uid = int(init.get("uid") or 0)
        room_id = int(init.get("room_id") or 0)
        info = {}
        if uid:
            infos, _ = client.fetch_statuses([uid])
            info = infos.get(uid) or {}
        if info:
            out = _room_from_info(info)
            out["room_id"] = room_id or out["room_id"]
            return out
        return {
            "uid": uid,
            "room_id": room_id,
            "name": "",
            "uname": "",
            "title": "",
            "live_status": int(init.get("live_status") or 0),
        }

    # 不是 room_id，再当成 uid 试一次
    uid = int(ident)
    infos, errors = client.fetch_statuses([uid])
    info = infos.get(uid)
    if not info:
        raise BiliError(f"无法解析「{text}」" + (f"（{errors[0]}）" if errors else ""))
    return _room_from_info(info)


def _room_from_info(info: dict) -> dict:
    return {
        "uid": int(info.get("uid") or 0),
        "room_id": int(info.get("room_id") or 0),
        "uname": info.get("uname") or "",
        "title": info.get("title") or "",
        "live_status": int(info.get("live_status") or 0),
        "name": info.get("uname") or "",
    }


def now() -> float:
    return time.time()
