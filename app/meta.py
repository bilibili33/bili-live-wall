"""后台配置页的表单元数据。

这里的每一项都必须和 config.SCHEMA 对得上（启动时会自检），
这样后台就不会漏掉任何一个可调项。
"""
from __future__ import annotations

from .config import SCHEMA

SECTIONS: list[dict] = [
    {
        "key": "server",
        "title": "服务",
        "desc": "监听地址与端口。改端口需要重启服务。",
        "fields": [
            {"key": "host", "label": "监听地址", "type": "text", "help": "默认 127.0.0.1 只允许本机访问；改成 0.0.0.0 可供局域网其它设备访问。"},
            {"key": "port", "label": "端口", "type": "number", "unit": "", "help": "监控页 http://127.0.0.1:<端口>/ ，配置页 /admin 。"},
            {"key": "open_browser", "label": "启动时自动打开浏览器", "type": "bool"},
        ],
    },
    {
        "key": "grid",
        "title": "画面布局",
        "desc": "宫格的行列数、间距和圆角。这一组只影响监控页的显示。",
        "fields": [
            {"key": "cols", "label": "列数", "type": "number", "unit": "列"},
            {"key": "rows", "label": "行数", "type": "number", "unit": "行"},
            {"key": "gap", "label": "格子间距", "type": "number", "unit": "px", "help": "硬盘录像机那种几乎贴在一起的感觉用 2~4。"},
            {"key": "radius", "label": "圆角", "type": "number", "unit": "px", "help": "想要方正的监控画面感就用 0~4。"},
            {"key": "background", "label": "背景色", "type": "color", "help": "格子缝隙露出来的颜色。"},
            {"key": "fit", "label": "画面填充", "type": "select", "options": [["cover", "cover（铺满并裁切）"], ["contain", "contain（完整显示留黑边）"]]},
        ],
    },
    {
        "key": "status",
        "title": "状态监控",
        "desc": "只发一个 API 请求就能拿到全部房间开播状态/标题/人气，很轻，间隔可以小。",
        "fields": [
            {"key": "enabled", "label": "启用状态监控", "type": "bool"},
            {"key": "interval", "label": "刷新间隔", "type": "number", "unit": "秒"},
            {"key": "timeout", "label": "请求超时", "type": "number", "unit": "秒"},
            {"key": "batch_size", "label": "单请求房间数", "type": "number", "unit": "个", "help": "一次请求里放多少个房间，超出的会拆成多次请求。"},
            {"key": "stale_after", "label": "数据过期阈值", "type": "number", "unit": "秒", "help": "超过这么久没成功刷新，监控页把该窗口标记为数据陈旧。"},
            {"key": "api_base", "label": "接口域名", "type": "text"},
            {"key": "user_agent", "label": "User-Agent", "type": "text"},
            {"key": "referer", "label": "Referer", "type": "text"},
            {"key": "cookie", "label": "Cookie（可选）", "type": "textarea", "help": "留空即可。若遇到风控/需要登录态，可填 SESSDATA=xxx; bili_jct=xxx。"},
        ],
    },
    {
        "key": "capture",
        "title": "截图抓取",
        "desc": "独立线程，与状态监控完全分开。抓的是 B 站直播间的实时关键帧图。",
        "fields": [
            {"key": "enabled", "label": "启用截图", "type": "bool"},
            {"key": "interval", "label": "截图间隔", "type": "number", "unit": "秒", "help": "比状态监控慢一些即可，比如 20~60 秒。"},
            {"key": "timeout", "label": "下载超时", "type": "number", "unit": "秒"},
            {"key": "only_when_live", "label": "只截直播中的房间", "type": "bool", "help": "关掉后未开播的房间会抓房间封面图。"},
            {"key": "skip_identical", "label": "画面没变就不刷新", "type": "bool", "help": "关键帧内容一样时不重写文件，避免画面无谓闪动。"},
        ],
    },
    {
        "key": "ui",
        "title": "画面叠加信息",
        "desc": "监控页每个窗口上叠什么字。",
        "fields": [
            {"key": "show_title", "label": "显示直播标题", "type": "bool"},
            {"key": "show_anchor", "label": "显示主播名", "type": "bool"},
            {"key": "show_online", "label": "显示人气值", "type": "bool"},
            {"key": "show_status_dot", "label": "显示状态圆点", "type": "bool"},
            {"key": "show_channel", "label": "显示通道号 CH", "type": "bool", "help": "用工作区那套数字图片绘制，左上角。"},
            {"key": "show_timestamp", "label": "显示截图时间", "type": "bool", "help": "右下角，同样用数字图片绘制。"},
            {"key": "title_lines", "label": "标题最多几行", "type": "number", "unit": "行"},
            {"key": "title_size", "label": "标题字号", "type": "number", "unit": "px", "help": "会随窗口宽度自动缩放，这里是 1080p 下的基准值。"},
            {"key": "refresh_interval", "label": "页面轮询间隔", "type": "number", "unit": "秒", "help": "监控页多久向服务端要一次最新状态。"},
            {"key": "offline_dim", "label": "未开播压暗程度", "type": "number", "unit": "0~1", "help": "0 全黑，1 不压暗。"},
            {"key": "live_accent", "label": "直播中边框颜色", "type": "color"},
            {"key": "live_accent_width", "label": "直播中边框粗细", "type": "number", "unit": "px", "help": "0 表示不要边框。"},
            {"key": "clock_24h", "label": "24 小时制", "type": "bool"},
            {"key": "osd_scale", "label": "数字 OSD 缩放", "type": "number", "unit": "倍"},
        ],
    },
    {
        "key": "assets",
        "title": "素材路径",
        "desc": "相对路径按项目根目录解析，也可以填绝对路径。",
        "fields": [
            {"key": "fallback_image", "label": "未开播底图", "type": "text", "help": "没在直播时铺在这个窗口里的图片，按 cover 裁切、不拉伸。"},
            {"key": "digit_dir", "label": "数字图片目录", "type": "text", "help": "里面放 0.png ~ 9.png（透明底白字）。"},
        ],
    },
    {
        "key": "storage",
        "title": "存储",
        "desc": "截图与状态缓存落盘位置。",
        "fields": [
            {"key": "shot_dir", "label": "截图目录", "type": "text"},
            {"key": "state_file", "label": "状态缓存文件", "type": "text", "help": "重启后先显示上次的画面，再等新一轮刷新。"},
            {"key": "log_lines", "label": "后台日志保留条数", "type": "number", "unit": "行"},
        ],
    },
]

ROOM_PER_PAGE = 200


def self_check() -> list[str]:
    """确保每个配置项都在后台出现。"""
    problems: list[str] = []
    covered: dict[str, set[str]] = {}
    for section in SECTIONS:
        covered[section["key"]] = {f["key"] for f in section["fields"]}
    for section, fields in SCHEMA.items():
        if section not in covered:
            problems.append(f"后台缺少配置分组：{section}")
            continue
        missing = set(fields) - covered[section]
        if missing:
            problems.append(f"后台缺少配置项：{section}.{', '.join(sorted(missing))}")
        extra = covered[section] - set(fields)
        if extra:
            problems.append(f"后台出现未知配置项：{section}.{', '.join(sorted(extra))}")
    return problems
