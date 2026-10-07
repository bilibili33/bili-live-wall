"""路径解析：区分"只读资源"和"用户数据"。

源码运行时两者都是项目根目录；用 PyInstaller 打包后，
资源（网页、素材）在 exe 内部的 `_internal` 里，
而 config.json 和截图必须写在 exe 旁边，用户才看得见、删得掉。
"""
from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> str:
    """只读资源所在的目录（打包后是解包目录 _internal）。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    # app/paths.py -> app -> 项目根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_root(default: str | None = None) -> str:
    """可写的用户数据目录（打包后是 exe 所在目录）。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    if default:
        return os.path.abspath(default)
    return bundle_dir()


def find_asset(relative: str, root: str) -> str:
    """先看用户目录里有没有同名文件（方便自己替换素材），再退回打包内置的那份。"""
    if not relative:
        return root
    if os.path.isabs(relative):
        return os.path.normpath(relative)
    user = os.path.normpath(os.path.join(root, relative))
    if os.path.exists(user):
        return user
    bundled = os.path.normpath(os.path.join(bundle_dir(), relative))
    if os.path.exists(bundled):
        return bundled
    return user
