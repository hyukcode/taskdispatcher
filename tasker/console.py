from __future__ import annotations

import os
import sys
import threading
from typing import Callable

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
            handle = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(handle, ctypes.byref(mode)):
                # 只打开 VT processing，保留控制台已有的输入/输出标志。
                k.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass


_enabled = None


def _use_color() -> bool:
    global _enabled
    if _enabled is None:
        if os.environ.get("NO_COLOR"):
            _enabled = False
        elif os.environ.get("FORCE_COLOR"):
            _enabled = True
        elif os.environ.get("TERM", "").lower() == "dumb":
            _enabled = False
        else:
            _enable_vt()
            try:
                _enabled = sys.stdout.isatty()
            except Exception:
                _enabled = False
    return _enabled


def paint(text: str, *styles: str) -> str:
    if not _use_color():
        return text
    codes = "".join(_C[s] for s in styles if s in _C)
    if not codes:
        return text
    return f"{codes}{text}{_C['reset']}"


OutputHook = Callable[[str], None]
_write_hook: OutputHook | None = None
_output_lock = threading.RLock()


def set_output_hook(hook: OutputHook | None) -> OutputHook | None:
    """安装输出接收器并返回之前的接收器。"""
    global _write_hook
    previous = _write_hook
    _write_hook = hook
    return previous


def restore_output_hook(hook: OutputHook, previous: OutputHook | None) -> None:
    """仅在当前接收器仍是 ``hook`` 时恢复，避免覆盖其他 TUI。"""
    global _write_hook
    if _write_hook is hook:
        _write_hook = previous


def _out(text: str, stream=None, **kw) -> None:
    # TUI 活跃时 stdout/stderr 都走同一条受锁保护的路径，避免 error 输出
    # 绕过输入行重绘而破坏终端状态。
    if _write_hook is not None:
        _write_hook(text)
        return
    target = stream or sys.stdout
    try:
        with _output_lock:
            target.write(text + "\n")
            target.flush()
    except (BrokenPipeError, OSError):
        # 管道下游提前退出时，CLI 不应因为输出失败再抛一层异常。
        pass


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
