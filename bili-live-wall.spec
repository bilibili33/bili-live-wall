# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir，不是单文件）。

用法：
    python -m PyInstaller --noconfirm --clean bili-live-wall.spec

产物在 dist/bili-live-wall/，整个文件夹一起发，不能只拿 exe。
"""
import os

HERE = os.path.abspath(SPECPATH)  # noqa: F821 - PyInstaller 注入的全局变量

a = Analysis(  # noqa: F821
    [os.path.join(HERE, "main.py")],
    pathex=[HERE],
    binaries=[],
    # 网页和素材打进 _internal，用户数据（config.json / data）不在这里
    datas=[
        (os.path.join(HERE, "app", "web"), "app/web"),
        (os.path.join(HERE, "statics"), "statics"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 只用标准库，用不到的都排掉，包能小一圈
    excludes=[
        "tkinter", "unittest", "doctest", "pydoc", "idlelib", "lib2to3",
        "test", "distutils", "setuptools", "pip", "pkg_resources",
        "numpy", "PIL", "matplotlib", "pandas", "scipy",
        "sqlite3", "curses", "multiprocessing",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="bili-live-wall",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 留个控制台，方便看日志和 Ctrl+C 退出
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="bili-live-wall",
)
