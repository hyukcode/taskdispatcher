
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
