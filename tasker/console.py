"""终端彩色输出助手（跨平台，自动启用 Windows VT 序列）。"""
from __future__ import annotations

import os
import sys

# 强制 stdout/stderr 使用 UTF-8，避免中文 + emoji 在 GBK 等窄编码终端上抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

_C = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "grey": "\033[90m",
}


def _enable_vt() -> None:
    if os.name == "nt":
        try:
            import ctypes

            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass


_enabled = None


def _use_color() -> bool:
    global _enabled
    if _enabled is None:
        if os.environ.get("NO_COLOR"):
            _enabled = False
        else:
            _enable_vt()
            _enabled = sys.stdout.isatty()
    return _enabled


def paint(text: str, *styles: str) -> str:
    if not _use_color():
        return text
    codes = "".join(_C[s] for s in styles if s in _C)
    return f"{codes}{text}{_C['reset']}"


def _out(text: str, stream=None, **kw) -> None:
    """统一出口：flush，保证管道/重定向下实时可见。"""
    (stream or sys.stdout).write(text + "\n")
    (stream or sys.stdout).flush()


def banner(text: str) -> None:
    _out(paint("═══════════════════════════════════════════════", "cyan"))
    _out(paint(text, "bold", "cyan"))
    _out(paint("═══════════════════════════════════════════════", "cyan"))


def info(text: str) -> None:
    _out(paint(text, "cyan"))


def step(text: str) -> None:
    _out(paint(f"▸ {text}", "bold", "blue"))


def ok(text: str) -> None:
    _out(paint(f"✓ {text}", "green"))


def warn(text: str) -> None:
    _out(paint(f"⚠ {text}", "yellow"))


def error(text: str) -> None:
    _out(paint(f"✗ {text}", "red"), stream=sys.stderr)


def dim(text: str) -> None:
    _out(paint(text, "dim", "grey"))


def event_line(summary: str, source: str = "") -> None:
    tag = paint(f"[{source}]", "magenta") if source else ""
    _out(f"  {tag} {summary}")


def status_line(icon: str, text: str, color: str = "cyan") -> None:
    _out(paint(f"{icon} {text}", color))
