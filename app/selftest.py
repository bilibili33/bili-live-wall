"""自检：一条命令把网络、证书、接口、图片 CDN、写盘全测一遍。

出问题的机器上跑 `bili-live-wall.exe --selftest`（或 `python main.py --selftest`），
把输出整段发回来就能定位问题在哪一层。
"""
from __future__ import annotations

import os
import socket
import ssl
import sys
import time
import urllib.parse

from .logging_ import describe_exception

HOSTS = ("api.live.bilibili.com", "i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com")

OK = "  [OK]  "
BAD = "  [!!]  "
INFO = "        "

_report: list[str] = []


def _print(text: str = "") -> None:
    _report.append(text)
    try:
        print(text, flush=True)
    except Exception:  # noqa: BLE001
        pass


def save_report(directory: str) -> str:
    """把整份自检报告存成文件（带 BOM 的 UTF-8，记事本直接打开不乱码）。"""
    import datetime
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(directory, f"selftest-{stamp}.txt")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig") as fh:
            fh.write("\n".join(_report))
            fh.write("\n")
        return path
    except OSError:
        return ""


def _step_dns(host: str) -> dict:
    started = time.time()
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({item[4][0] for item in infos})
        _print(f"{OK}DNS   {host} → {', '.join(ips)}  ({time.time() - started:.3f}s)")
        return {"ok": True, "ips": ips}
    except Exception as exc:  # noqa: BLE001
        _print(f"{BAD}DNS   {host} 解析失败：{type(exc).__name__}: {exc}")
        return {"ok": False, "error": describe_exception(exc)}


def _step_tcp_tls(host: str) -> dict:
    started = time.time()
    try:
        raw = socket.create_connection((host, 443), timeout=8)
    except Exception as exc:  # noqa: BLE001
        _print(f"{BAD}TCP   {host}:443 连不上：{describe_exception(exc)}")
        return {"ok": False, "error": describe_exception(exc)}
    tcp_ms = (time.time() - started) * 1000
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            cert = tls.getpeercert() or {}
            subject = dict(x[0] for x in cert.get("subject", ())).get("commonName", "?")
            issuer = dict(x[0] for x in cert.get("issuer", ())).get("commonName", "?")
            _print(f"{OK}TLS   {host} 握手成功  TLS={tls.version()}  "
                   f"证书 CN={subject}  签发者={issuer}  到期={cert.get('notAfter', '?')}  "
                   f"（TCP {tcp_ms:.0f}ms）")
            return {"ok": True}
    except ssl.SSLCertVerificationError as exc:
        _print(f"{BAD}TLS   {host} 证书校验失败：{exc}")
        _print(f"{INFO}      这台机器的根证书库可能不全，或者有代理/抓包软件在替换证书")
        return {"ok": False, "error": "证书校验失败"}
    except Exception as exc:  # noqa: BLE001
        _print(f"{BAD}TLS   {host} 握手失败：{describe_exception(exc)}（{exc}）")
        return {"ok": False, "error": describe_exception(exc)}
    finally:
        try:
            raw.close()
        except OSError:
            pass


def _step_http(client, url: str, what: str, with_referer: bool = True) -> dict:
    started = time.time()
    try:
        blob, info = client.probe(url, 15, with_referer, what)
        _print(f"{OK}HTTP  {what} {info['status']} {len(blob)}B "
               f"{info['content_type'] or '?'} ({time.time() - started:.2f}s)")
        return {"ok": True, "bytes": len(blob), "blob": blob, "info": info}
    except Exception as exc:  # noqa: BLE001
        _print(f"{BAD}HTTP  {what} 失败：{exc}")
        return {"ok": False, "error": str(exc)}


def run(runtime, first_room_only: bool = True) -> int:
    """跑一遍完整自检，返回退出码（0 全通过，1 有失败）。"""
    import urllib.request

    from .bili import host_variants

    cfg = runtime.config.snapshot()
    problems: list[str] = []
    notes: list[str] = []

    _print()
    _print("=" * 68)
    _print("  B 站直播监控墙 · 自检")
    _print("=" * 68)

    # ---- 1. 环境 -----------------------------------------------------
    _print()
    _print("【1】运行环境")
    for line in runtime.environment_lines():
        _print(line)
        runtime.sink.write("info", line, to_console=False)   # 也写进日志文件

    try:
        from .tls import ca_report, system_store_check
        report = ca_report(runtime.root)
        if report["bundled_loaded"]:
            _print(f"{OK}内置 CA 包已加载：{report['ca_bundle']}"
                   f"（{report['bundled_certs']} 个根证书）")
        else:
            _print(f"{BAD}内置 CA 包没加载成功，找的是 {report['searched']}")
            problems.append("内置 CA 包缺失")
    except Exception as exc:  # noqa: BLE001
        _print(f"{BAD}加载内置 CA 包出错：{exc}")
        system_store_check = None  # type: ignore[assignment]

    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001
        proxies = {}
    if proxies:
        _print(f"{INFO}注意：系统设置了代理 {proxies}")
        _print(f"{INFO}      程序会自动走这个代理，代理不通的话下面所有请求都会失败")

    # ---- 2. 网络分层探测 ---------------------------------------------
    _print()
    _print("【2】网络连通性（DNS / TCP / TLS）")
    reachable: dict[str, bool] = {}
    store_problem: list[str] = []
    for host in HOSTS:
        _print(f"{INFO}--- {host} ---")
        dns = _step_dns(host)
        if not dns["ok"]:
            reachable[host] = False
            problems.append(f"{host} DNS 解析失败")
            continue
        tls_step = _step_tcp_tls(host)
        reachable[host] = tls_step["ok"]
        if not tls_step["ok"]:
            problems.append(f"{host} 连接/证书失败：{tls_step.get('error')}")
        if system_store_check is not None:
            ok_store, why = system_store_check(host)
            if ok_store:
                _print(f"{OK}系统库  {host} {why}")
            else:
                _print(f"{BAD}系统库  {host} {why}")
                _print(f"{INFO}      这台的系统根证书库认不出这个域名 —— "
                       f"程序已用内置 CA 包兜底，但其它软件（浏览器除外）可能也会受影响")
                store_problem.append(host)
    if store_problem:
        notes.append("系统根证书库认不出 " + ", ".join(store_problem)
                     + "（程序已用内置 CA 包兜底，不影响截图；但别的软件可能会受影响，"
                       "建议跑一次 Windows Update 更新根证书）")

    # ---- 3. 接口 -----------------------------------------------------
    _print()
    _print("【3】B 站接口")
    rooms = runtime.state.rooms()
    if not rooms:
        _print(f"{BAD}配置里没有任何房间，先在配置后台加一个")
        problems.append("没有配置房间")
        room = None
    else:
        room = rooms[0]
        _print(f"{INFO}用第一个房间做测试：{room.display_name} (uid={room.uid})")

    client = runtime.bili
    cover_url = ""
    keyframe_url = ""
    if room is not None:
        infos, errors = client.fetch_statuses([room.uid])
        info = infos.get(room.uid)
        if not info:
            _print(f"{BAD}状态接口没有返回该房间：{errors}")
            problems.append("状态接口异常")
        else:
            status = int(info.get("live_status") or 0)
            _print(f"{OK}状态接口正常：live_status={status} "
                   f"标题={info.get('title')!r} 主播={info.get('uname')!r}")
            cover_url = (info.get("cover_from_user") or info.get("cover") or "").strip()
            keyframe_url = (info.get("keyframe") or "").strip()
            _print(f"{INFO}keyframe 字段：{keyframe_url or '（空，说明当前没在播或接口没给）'}")
            _print(f"{INFO}封面字段：{cover_url or '（空）'}")

    # ---- 4. 图片 CDN -------------------------------------------------
    _print()
    _print("【4】图片 CDN（这一步就是截图失败的地方）")
    test_url = keyframe_url or cover_url
    if not test_url:
        _print(f"{BAD}拿不到任何图片地址，没法测")
        problems.append("没有可测的图片地址")
    else:
        _print(f"{INFO}测试地址：{test_url}")
        _print(f"{INFO}--- 依次尝试各个 CDN 域名 ---")
        got: list[tuple[str, int]] = []
        for variant in host_variants(test_url):
            host = urllib.parse.urlsplit(variant).netloc
            res = _step_http(client, variant, f"图片@{host}", True)
            if res["ok"]:
                blob = res.get("blob") or b""
                kind = "JPEG" if blob[:3] == b"\xff\xd8\xff" else \
                       "PNG" if blob[:4] == b"\x89PNG" else \
                       "WEBP" if blob[8:12] == b"WEBP" else "未知格式"
                _print(f"{INFO}      实际拿到 {len(blob)} 字节，识别为 {kind}")
                got.append((host, len(blob)))
            else:
                _print(f"{INFO}--- 去掉 Referer 再试一次 {host} ---")
                res2 = _step_http(client, variant, f"图片@{host}(无Referer)", False)
                if res2["ok"]:
                    got.append((host, res2.get("bytes", 0)))

        if got:
            _print(f"{OK}至少有一个 CDN 能取到图片：{got}")
        else:
            _print(f"{BAD}所有 CDN 都取不到图片，截图功能会一直是底图")
            problems.append("所有图片 CDN 都失败")

    # ---- 5. 写盘 -----------------------------------------------------
    _print()
    _print("【5】写盘")
    shot_dir = runtime.config.shot_dir
    try:
        os.makedirs(shot_dir, exist_ok=True)
        probe = os.path.join(shot_dir, "_selftest.tmp")
        with open(probe, "wb") as fh:
            fh.write(b"ok")
        size = os.path.getsize(probe)
        os.remove(probe)
        _print(f"{OK}截图目录可写：{shot_dir}（写入 {size} 字节并删除成功）")
    except OSError as exc:
        _print(f"{BAD}截图目录不可写：{shot_dir} —— {type(exc).__name__}: {exc}")
        _print(f"{INFO}      常见原因：程序放在 C:\\Program Files 之类需要管理员权限的目录，"
               f"或者压缩包是从只读位置解压的")
        problems.append("截图目录不可写")

    # ---- 6. 结论 -----------------------------------------------------
    _print()
    _print("=" * 68)
    if notes:
        _print("  提示（不影响功能）：")
        for item in notes:
            _print(f"    · {item}")
        _print()
    if not problems:
        _print("  自检结论：能测的都通过了。如果监控页还是只有底图，把日志文件发我看。")
    else:
        _print("  自检结论：发现问题 ——")
        for item in problems:
            _print(f"    · {item}")
        _print()
        api_ok = reachable.get("api.live.bilibili.com", False)
        cdn_ok = any(reachable.get(h, False) for h in ("i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"))
        if api_ok and not cdn_ok:
            _print("  症状很像：接口通、图片 CDN 不通。")
            _print("  可以试试把 DNS 换成 223.5.5.5 / 119.29.29.29，"
                   "或者用手机热点验证一下是不是网络的问题。")
        elif not api_ok and not cdn_ok:
            _print("  接口和图片都不通，更像是这台机器的网络/DNS/代理有问题。")
    _print(f"  日志文件：{runtime.sink.path or '（未启用）'}")
    _print("=" * 68)

    report_dir = os.path.dirname(runtime.sink.path or runtime.config.state_file)
    saved = save_report(report_dir)
    if saved:
        _print(f"  自检报告已保存：{saved}")
        _print("  ↑ 出问题时把这个文件发给我就行")
        try:
            print(f"\n自检报告已保存到：{saved}", flush=True)
        except Exception:  # noqa: BLE001
            pass
    _print()
    return 1 if problems else 0
