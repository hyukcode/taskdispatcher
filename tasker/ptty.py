"""ptty attach：把终端直接交给 claude/codex 的交互 TUI（macOS/Linux）。

为什么需要它：claude -p / codex exec 在 headless 下不出现权限弹窗，
真正的"审批请求"交互界面只在交互式 TUI 里有。attach 模式用 pty fork 出
交互式进程，原样转发其字节流到你自己的终端 —— 你能亲眼看到并直接回答审批，
也能像用原版 CLI 一样继续对话。

注意：
- 仅 macOS / Linux（Windows 无 pty）。
- 与调度器并发不兼容：attach 是"独占终端"模式，一般单独运行。
- 这是尽力而为转发，不解析 TUI 的 ANSI 光标控制；依赖目标 CLI 的交互界面。
"""
from __future__ import annotations

import os
import sys

from .spawn import resolve_binary


def run_attached(binary: str, workdir: str, prompt: str = "", raw_log: str = "") -> int:
    if os.name == "nt":
        print("❌ ptty attach 仅支持 macOS/Linux（Windows 无 pty 支持）", file=sys.stderr)
        return 2

    import select
    import termios
    import tty

    command = [resolve_binary(binary)]
    if prompt:
        command.append(prompt)

    pid, master_fd = os.forkpty()
    if pid == 0:
        # 子进程
        try:
            os.chdir(workdir)
            os.execvp(command[0], command)
        except Exception as e:  # noqa: BLE001
            print(f"启动失败: {e}", file=sys.stderr)
            os._exit(127)

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    old = termios.tcgetattr(stdin_fd)
    try:
        tty.setraw(stdin_fd)
        logf = open(raw_log, "wb", buffering=0) if raw_log else None
        status = 0
        while True:
            try:
                r, _, _ = select.select([master_fd, stdin_fd], [], [])
            except OSError:
                break
            for fd in r:
                if fd == master_fd:
                    try:
                        data = os.read(master_fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        _, st = os.waitpid(pid, os.WNOHANG)
                        status = os.waitstatus_to_exitcode(st) if st else 0
                        return status
                    os.write(stdout_fd, data)
                    if logf:
                        logf.write(data)
                else:
                    try:
                        data = os.read(stdin_fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        continue
                    try:
                        os.write(master_fd, data)
                    except OSError:
                        return status
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old)
        try:
            os.close(master_fd)
        except OSError:
            pass
    return 0
