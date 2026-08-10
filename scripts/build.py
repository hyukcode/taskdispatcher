#!/usr/bin/env python3
"""用 PyInstaller 把 tasker 打成独立可执行文件 / 目录。

用法（在目标平台本机执行 —— PyInstaller 不能跨平台交叉编译）：
  pip install pyinstaller
  python scripts/build.py              # 默认 --onedir（推荐，启动快）
  python scripts/build.py --onefile    # 单个可执行文件
  python scripts/build.py --windowed   # 不打控制台（一般不要，我们就要控制台）

产物在 dist/ ：
  - onedir:  dist/tasker/tasker[.exe]（整个文件夹拷到目标机）
  - onefile: dist/tasker[.exe]

重要：
- 打包只包含 Python 代码，**不含 claude / codex CLI**。目标机仍需单独安装：
      brew install --cask claude            # macOS 装 Claude Code
      npm install -g @openai/codex          # 或 Codex
- macOS 的二进制必须在 macOS 上构建；Windows 的 .exe 必须在 Windows 上构建。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def main() -> int:
    args = sys.argv[1:]
    onefile = "--onefile" in args
    windowed = "--windowed" in args

    if shutil.which("pyinstaller") is None:
        print("未安装 PyInstaller，先执行:  pip install pyinstaller", file=sys.stderr)
        return 1

    cmd = [
        "pyinstaller",
        "--noconfirm",
        "--clean",
        "--name", "tasker",
        "--console" if not windowed else "--windowed",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(ROOT),
    ]
    if onefile:
        cmd.append("--onefile")

    entry = ROOT / "tasker_cli.py"
    cmd.append(str(entry))

    print("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)

    if onefile:
        out = DIST / ("tasker.exe" if sys.platform.startswith("win") else "tasker")
        print(f"✅ 单文件可执行: {out}")
        print(f"   拷到目标机后运行: {out.name} --help")
    else:
        out = DIST / "tasker"
        print(f"✅ 目录: {out}")
        print(f"   把整个 {out.name}/ 文件夹拷到目标机，运行: {out.name} --help")
    print("注意：目标机仍需自行安装 claude / codex CLI。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
