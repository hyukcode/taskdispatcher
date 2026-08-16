#!/usr/bin/env python3
"""离线构建纯 Python wheel（只依赖标准库 zipfile/hashlib，无需网络、无需 wheel/setuptools）。

为什么需要它：目标机可能没网，且有些机器没装 wheel 包，导致 pip 无法离线构建。
本脚本直接产出一个标准 wheel 文件：
    dist/tasker-0.1.0-py3-none-any.whl
在任意 Python 3.9+ 机器上执行
    pip install tasker-0.1.0-py3-none-any.whl
即可装出 `tasker` 命令（wheel 安装不触发构建、不需要网络）。

用法：
    python scripts/build_wheel.py
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

# 输出强制 UTF-8，避免 Windows GBK 终端中文乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "tasker"
DIST = ROOT / "dist"
# 发布名（PyPI 唯一可用名；tasker/claude-codex-tasker 均被占用或不可用）
NAME = "multicc"
VERSION = "0.5.4"
DIST_INFO = f"{NAME}-{VERSION}.dist-info"


def _metadata() -> str:
    # 有意不发 description / long_description，让 PyPI 页面不显示工具内容
    lines = ["Metadata-Version: 2.1", f"Name: {NAME}", f"Version: {VERSION}"]
    lines.append("Requires-Python: >=3.9")
    lines.append("License: MIT")
    # 运行时依赖必须声明，否则 pip 从 PyPI 安装时不会自动拉取（尤其 tp-wy 模板包）
    lines.append("Requires-Dist: claude-agent-sdk")
    lines.append("Requires-Dist: tp-wy (>=0.2.0)")
    return "\n".join(lines) + "\n"

WHEEL = """\
Wheel-Version: 1.0
Generator: tasker.build_wheel (stdlib-only)
Root-Is-Purelib: true
Tag: py3-none-any
"""

ENTRY_POINTS = """\
[console_scripts]
tasker = tasker.main:main
"""


def _all_py_files() -> list[str]:
    out = []
    for p in sorted(PKG.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        out.append(rel)
    return out


def _hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build() -> Path:
    DIST.mkdir(exist_ok=True)
    whl = DIST / f"{NAME}-{VERSION}-py3-none-any.whl"
    files = _all_py_files()
    records: list[tuple[str, str, str]] = []

    with zipfile.ZipFile(whl, "w", zipfile.ZIP_DEFLATED) as zf:
        # 包内模块
        for rel in files:
            data = (ROOT / rel.replace("/", os.sep)).read_bytes()
            records.append((rel, _hash(data), str(len(data))))
            zf.writestr(rel, data)
        # 注入版本号文件（安装后 __init__.py 优先读它，避免读不到 pyproject.toml 而显示 0.0.0）
        ver_rel = "tasker/_version.py"
        ver_data = f'__version__ = "{VERSION}"\n'.encode("utf-8")
        records.append((ver_rel, _hash(ver_data), str(len(ver_data))))
        zf.writestr(ver_rel, ver_data)
        # dist-info 元数据
        for name, content in [
            ("METADATA", _metadata()),
            ("WHEEL", WHEEL),
            ("entry_points.txt", ENTRY_POINTS),
        ]:
            data = content.encode("utf-8")
            records.append((f"{DIST_INFO}/{name}", _hash(data), str(len(data))))
            zf.writestr(f"{DIST_INFO}/{name}", data)
        # RECORD（自身留空，这是规范要求）
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        for path, h, size in records:
            w.writerow([path, f"sha256={h}", size])
        w.writerow([f"{DIST_INFO}/RECORD", "", ""])
        zf.writestr(f"{DIST_INFO}/RECORD", buf.getvalue().encode("utf-8"))

    print(f"[OK] wheel 已生成: {whl}")
    print(f"[OK] 共 {len(files)} 个模块，纯 Python，py3-none-any。")
    print(f"     目标机执行:  pip install {NAME}-{VERSION}-py3-none-any.whl")
    return whl


if __name__ == "__main__":
    build()
