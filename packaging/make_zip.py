"""打发布 zip。

不用 PowerShell 的 Compress-Archive：它写出来的条目用反斜杠当分隔符，
Windows 资源管理器能忍，但 macOS / Linux 解压会得到一堆名字里带 \\ 的文件。
这里统一用正斜杠。
"""
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def build(stage: str, out_zip: str) -> None:
    stage = os.path.abspath(stage)
    top = os.path.basename(stage)
    count = 0
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for base, dirs, files in os.walk(stage):
            dirs.sort()
            files.sort()
            rel_dir = os.path.relpath(base, stage)
            arc_dir = top if rel_dir == "." else f"{top}/{rel_dir.replace(os.sep, '/')}"
            if rel_dir != ".":
                z.writestr(arc_dir + "/", b"")     # 显式目录项
            for name in files:
                full = os.path.join(base, name)
                arc = f"{arc_dir}/{name}"
                z.write(full, arc)
                count += 1
    print(f"  写入 {count} 个文件 → {out_zip}")
    print(f"  体积 {os.path.getsize(out_zip) / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    build(sys.argv[1], sys.argv[2])
