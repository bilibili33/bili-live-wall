"""TLS：用「内置 CA 包 + 系统根证书库」的并集来校验证书。

为什么要这么做：
  api.live.bilibili.com 的证书链挂在 GlobalSign R3 根上，
  而 i0/i1/i2.hdslb.com（关键帧图）挂在 GlobalSign R46 根上 —— 两套根不一样。
  如果目标机器的系统根证书库比较旧（关掉了 Windows Update、LTSC 版、
  长期离线的机器、某些精简版系统），就会出现非常诡异的现象：

      状态能拉到，但关键帧图一张都拉不到。

  内置一份 CA 包（certs/cacert.pem，来自 certifi，Mozilla 根证书列表）
  并和系统库并集使用，就能彻底摆脱对目标机器根证书库新鲜度的依赖。
"""
from __future__ import annotations

import os
import ssl
import threading
import urllib.request

from .paths import bundle_dir, find_asset

CA_RELATIVE = "certs/cacert.pem"

_lock = threading.RLock()
_context: ssl.SSLContext | None = None
_opener: urllib.request.OpenerDirector | None = None
_state: dict = {"ca_bundle": "", "bundled_loaded": False}


def ca_bundle_path(root: str) -> str:
    """先看程序目录里有没有用户自己放的 CA 包，再退回打包内置的那份。"""
    return find_asset(CA_RELATIVE, root)


def ssl_context(root: str) -> ssl.SSLContext:
    global _context
    with _lock:
        if _context is not None:
            return _context
        ctx = ssl.create_default_context()
        try:
            ctx.load_default_certs()          # 系统根证书库
        except Exception:  # noqa: BLE001
            pass
        bundled = ca_bundle_path(root)
        loaded = False
        if bundled and os.path.isfile(bundled):
            try:
                ctx.load_verify_locations(cafile=bundled)
                loaded = True
            except (OSError, ssl.SSLError):
                loaded = False
        _state["ca_bundle"] = bundled if loaded else ""
        _state["bundled_loaded"] = loaded
        _context = ctx
        return ctx


def opener(root: str) -> urllib.request.OpenerDirector:
    """所有 HTTP 请求都走这个 opener，它带上了我们的并集证书上下文。

    用 build_opener 而不是 urlopen，是为了保留默认的 ProxyHandler，
    这样系统代理设置仍然生效。
    """
    global _opener
    ctx = ssl_context(root)      # 先拿上下文，它自己会加锁，别套在下面的锁里
    with _lock:
        if _opener is None:
            _opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ctx))
        return _opener


def ca_report(root: str) -> dict:
    ssl_context(root)
    path = _state.get("ca_bundle", "")
    count = 0
    if path:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                count = fh.read().count("BEGIN CERTIFICATE")
        except OSError:
            count = 0
    return {
        "ca_bundle": path,
        "bundled_loaded": bool(_state.get("bundled_loaded")),
        "bundled_certs": count,
        "searched": os.path.join(bundle_dir(), CA_RELATIVE),
    }


def system_store_check(host: str, timeout: float = 8.0) -> tuple[bool, str]:
    """只用系统根证书库验证一次；返回 (是否通过, 说明)。

    用来判断"是不是这台机器的根证书库太旧"。
    """
    import socket

    try:
        ctx = ssl.create_default_context()      # 故意不加载内置 CA
    except Exception as exc:  # noqa: BLE001
        return False, f"建不了默认上下文：{exc}"
    try:
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                issuer = dict(x[0] for x in cert.get("issuer", ())).get("commonName", "?")
        return True, f"系统库可以验证（签发者 {issuer}）"
    except ssl.SSLCertVerificationError as exc:
        return False, f"系统库缺这个签发者的根证书：{exc.verify_message or exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败：{type(exc).__name__}: {exc}"
