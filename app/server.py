"""HTTP 服务：监控页、配置后台和它们的接口。"""
from __future__ import annotations

import json
import mimetypes
import os
import socket
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import meta
from .bili import BiliError, resolve_room, sniff_mime
from .config import ConfigStore, DEFAULTS
from .paths import bundle_dir
from .runtime import Runtime

WEB_DIR = os.path.join(bundle_dir(), "app", "web")
MAX_BODY = 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "BiliLiveWall/1.0"
    protocol_version = "HTTP/1.1"

    # -- 基础设施 -----------------------------------------------------
    @property
    def rt(self) -> Runtime:
        return self.server.runtime  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # 把访问日志压下去，控制台只留业务日志
        if self.server.verbose:  # type: ignore[attr-defined]
            print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None, head_only: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def _json(self, payload, code: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _error(self, code: int, message: str):
        self._json({"ok": False, "error": message}, code)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", "replace"))

    def _send_file(self, path: str, cache: str, fallback_mime: str | None = None, immutable: bool = False):
        """带 Last-Modified / ETag 的文件响应，支持 304。"""
        try:
            stat = os.stat(path)
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "文件不存在")
            return
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "文件读取失败")
            return
        ctype = fallback_mime or sniff_mime(blob)
        if ctype == "application/octet-stream":
            ctype = mimetypes.guess_type(path)[0] or ctype
        extra = {"Cache-Control": cache, "ETag": etag, "Last-Modified": self.date_time_string(stat.st_mtime)}
        self._send(HTTPStatus.OK, blob, ctype, extra)

    # -- 路由 ---------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html", "/monitor"):
                self._page("monitor.html")
            elif path in ("/admin", "/admin/", "/admin.html", "/config"):
                self._page("admin.html")
            elif path == "/api/state":
                self._json({"ok": True, "has_digits": len(self.rt.digits()) >= 10,
                            "paths": self._paths(), **self.rt.state.snapshot()})
            elif path == "/api/config":
                self._json(self._config_payload())
            elif path == "/api/log":
                limit = int((query.get("limit") or ["200"])[0] or 200)
                self._json({"ok": True, "lines": self.rt.state.logs(limit)})
            elif path == "/api/digits":
                self._json({"ok": True, "digits": sorted(self.rt.digits().keys())})
            elif path == "/assets/fallback.png":
                blob, ctype = self.rt.fallback_image()
                if not blob:
                    self._error(HTTPStatus.NOT_FOUND, "未找到未开播底图，请检查 assets.fallback_image")
                    return
                self._send(HTTPStatus.OK, blob, ctype, {"Cache-Control": "no-cache"})
            elif path.startswith("/assets/digits/"):
                self._digit(path.rsplit("/", 1)[-1])
            elif path.startswith("/shots/"):
                self._shot(path.rsplit("/", 1)[-1])
            elif path == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            else:
                self._error(HTTPStatus.NOT_FOUND, "没有这个路径")
        except Exception as exc:  # noqa: BLE001 - 兜底，别让连接悬着
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务端异常：{exc}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            payload = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, f"请求体无法解析：{exc}")
            return
        try:
            if path in ("/api/config", "/api/config/save"):
                self._save_config(payload)
            elif path == "/api/rooms/resolve":
                self._resolve(payload)
            elif path == "/api/refresh":
                self.rt.wake()
                self.rt.state.log("info", "手动触发了一轮刷新")
                self._json({"ok": True})
            elif path == "/api/test":
                self._test()
            elif path in ("/api/rooms/toggle", "/api/rooms/enable"):
                uid = int(payload.get("uid") or 0)
                enabled = bool(payload.get("enabled", True))
                if not self.rt.state.set_enabled(uid, enabled):
                    self._error(HTTPStatus.NOT_FOUND, "没有这个房间")
                    return
                self.rt.wake()
                self._json({"ok": True})
            else:
                self._error(HTTPStatus.NOT_FOUND, "没有这个接口")
        except BiliError as exc:
            self._error(HTTPStatus.BAD_GATEWAY, str(exc))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务端异常：{exc}")

    # -- 具体实现 -----------------------------------------------------
    def _page(self, name: str):
        path = os.path.join(WEB_DIR, name)
        if not os.path.isfile(path):
            self._error(HTTPStatus.NOT_FOUND, f"缺少页面文件 {name}")
            return
        with open(path, "rb") as fh:
            blob = fh.read()
        self._send(HTTPStatus.OK, blob, "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _digit(self, name: str):
        digit = os.path.splitext(name)[0]
        if not digit.isdigit():
            self._error(HTTPStatus.NOT_FOUND, "没有这个数字")
            return
        path = self.rt.digits().get(digit)
        if not path:
            self._error(HTTPStatus.NOT_FOUND, f"缺少数字图片 {digit}")
            return
        self._send_file(path, "public, max-age=86400")

    def _shot(self, name: str):
        uid = os.path.splitext(name)[0]
        if not uid.isdigit():
            self._error(HTTPStatus.NOT_FOUND, "房间号非法")
            return
        path = self.rt.shot_path(int(uid))
        if not os.path.isfile(path):
            blob, ctype = self.rt.fallback_image()
            self._send(HTTPStatus.OK, blob, ctype, {"Cache-Control": "no-store"})
            return
        # URL 里带了内容版本号，可以放心长缓存
        self._send_file(path, "public, max-age=31536000, immutable", fallback_mime="image/jpeg")

    def _paths(self) -> dict:
        shot_dir = self.rt.config.shot_dir
        try:
            shots = len([f for f in os.listdir(shot_dir) if f.endswith(".jpg")])
        except OSError:
            shots = 0
        return {
            "root": self.rt.root,
            "config_file": self.rt.config.path,
            "shot_dir": shot_dir,
            "state_file": self.rt.config.state_file,
            "log_file": self.rt.sink.path or "",
            "fallback_image": self.rt.config.fallback_image,
            "fallback_exists": os.path.isfile(self.rt.config.fallback_image),
            "digit_dir": self.rt.config.digit_dir,
            "digits": sorted(self.rt.digits().keys()),
            "shot_count": shots,
            "requests": self.rt.bili.requests,
            "request_failures": self.rt.bili.request_failures,
        }

    def _config_payload(self) -> dict:
        cfg = self.rt.config.snapshot()
        problems = meta.self_check()
        if problems:
            for item in problems:
                self.rt.state.log("error", f"后台配置项自检失败：{item}")
        return {
            "ok": True,
            "config": cfg,
            "defaults": DEFAULTS,
            "sections": meta.SECTIONS,
            "problems": problems,
            "paths": self._paths(),
        }

    def _save_config(self, payload: dict):
        incoming = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        incoming = {k: v for k, v in incoming.items() if k != "ok"}
        cfg, warnings = self.rt.apply_config(incoming)
        self.rt.state.log("info", "配置已保存" + (f"（{len(warnings)} 条提示）" if warnings else ""))
        for item in warnings:
            self.rt.state.log("warn", item)
        self._json({"ok": True, "config": cfg, "warnings": warnings})

    def _resolve(self, payload: dict):
        text = str(payload.get("input") or "").strip()
        if not text:
            self._error(HTTPStatus.BAD_REQUEST, "请输入直播间链接或房间号")
            return
        info = resolve_room(self.rt.bili, text)
        self._json({"ok": True, "room": info})

    def _test(self):
        started = time.time()
        rooms = self.rt.state.rooms()
        if not rooms:
            self._json({"ok": True, "message": "还没有房间可测", "elapsed": 0})
            return
        uids = [r.uid for r in rooms][:5]
        infos, errors = self.rt.bili.fetch_statuses(uids)
        self._json({
            "ok": not errors or bool(infos),
            "requested": len(uids),
            "returned": len(infos),
            "errors": errors,
            "elapsed": round(time.time() - started, 2),
        })


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # 注意：http.server.HTTPServer 默认 allow_reuse_address = 1。
    # 在 Windows 上 SO_REUSEADDR 的语义是"允许别的进程绑同一个端口"，
    # 于是双击两次 exe 会两个实例都"启动成功"，请求被随机分给其中一边，
    # 表现出来就是画面时有时无、配置改了没反应。这里关掉，让它老老实实报端口占用。
    allow_reuse_address = False

    def __init__(self, address, runtime: Runtime, verbose: bool = False):
        self.runtime = runtime
        self.verbose = verbose
        super().__init__(address, Handler)


def build(root: str, host: str | None = None, port: int | None = None,
          verbose: bool = False, console_level: str | None = None) -> tuple[Server, Runtime]:
    runtime = Runtime(root, console_level=console_level)
    cfg = runtime.config.snapshot()
    addr = (host or cfg["server"]["host"], int(port or cfg["server"]["port"]))
    server = Server(addr, runtime, verbose=verbose)
    return server, runtime


def serve(root: str, host: str | None = None, port: int | None = None,
          verbose: bool = False, open_browser: bool | None = None,
          console_level: str | None = None) -> int:
    try:
        server, runtime = build(root, host, port, verbose, console_level)
    except OSError as exc:
        busy = getattr(exc, "errno", None) in (48, 98, 10013, 10048, 10049)
        print(f"启动失败：{exc}")
        if busy:
            cfg = None
            try:
                cfg = ConfigStore(root).snapshot()
            except Exception:  # noqa: BLE001
                pass
            used_port = port or (cfg["server"]["port"] if cfg else 8848)
            print(f"端口 {used_port} 用不了。多半是已经开着一个实例了"
                  f"（看看任务栏/托盘里是不是已经有一个），或者被别的东西占用。")
            print(f"换个端口：bili-live-wall.exe --port 9000"
                  f"，或者改 config.json 里的 server.port。")
        return 1

    runtime.start()
    shown_host = "127.0.0.1" if server.server_address[0] in ("0.0.0.0", "::") else server.server_address[0]
    base = f"http://{shown_host}:{server.server_address[1]}"
    print("=" * 58)
    print("  B 站直播监控墙 已启动")
    print(f"  监控画面 : {base}/")
    print(f"  配置后台 : {base}/admin")
    print(f"  配置文件 : {runtime.config.path}")
    print("  按 Ctrl+C 退出")
    print("=" * 58, flush=True)

    cfg = runtime.config.snapshot()
    want_browser = cfg["server"]["open_browser"] if open_browser is None else open_browser
    if want_browser:
        def _open():
            time.sleep(1.2)
            try:
                webbrowser.open(base + "/")
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在退出……")
    finally:
        server.shutdown()
        server.server_close()
        runtime.stop()
    return 0


def pick_port(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((host, port)) != 0
