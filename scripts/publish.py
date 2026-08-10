#!/usr/bin/env python3
"""构建并发布 tasker 到 PyPI。

前置：
  pip install build twine        # 仅构建机需要，目标机不需要
  # 或离线：先 python scripts/build_wheel.py 生成 wheel 后直接 twine upload

上传需要 PyPI 账号令牌：
  export TWINE_USERNAME=__token__
  export TWINE_PASSWORD=pypi-xxxxx
或使用 ~/.pypirc。

用法：
  python scripts/publish.py               # 构建（含 sdist）并上传 PyPI 正式
  python scripts/publish.py --test        # 上传到 TestPyPI 试跑
  python scripts/publish.py --upload-only # 只上传已构建好的产物（离线构建时用）

发布后，任意目标机：
  pip install multicc
或指定版本：
  pip install multicc==0.1.0
升级：
  pip install -U multicc
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def _have_module(mod: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(mod) is not None


def main() -> int:
    args = sys.argv[1:]
    test = "--test" in args
    upload_only = "--upload-only" in args
    repo = "testpypi" if test else "pypi"

    if not upload_only:
        # 优先用离线脚本构建 wheel（不依赖 wheel/setuptools/网络）
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_wheel.py")], check=True, cwd=ROOT)
        # 若装了 build 模块则额外打 sdist（含 README/许可证的源码包）；否则跳过
        if _have_module("build"):
            subprocess.run([sys.executable, "-m", "build", "--sdist", "--outdir", str(DIST)], check=True, cwd=ROOT)
        else:
            print("[warn] 未安装 build 模块，跳过 sdist（仅上传 wheel）")

    if not _have_module("twine"):
        print("[err] 未安装 twine:  pip install twine", file=sys.stderr)
        return 1

    files = sorted(p for p in DIST.iterdir() if p.suffix in (".whl", ".tar.gz"))
    if not files:
        print(f"[err] {DIST} 中没有待上传的构建产物", file=sys.stderr)
        return 1

    cmd = [sys.executable, "-m", "twine", "upload", "--repository", repo] + [str(f) for f in files]
    print("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)
    print(f"[OK] 已发布到 {repo}: {', '.join(f.name for f in files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
