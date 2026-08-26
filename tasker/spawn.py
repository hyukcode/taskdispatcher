
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
from typing import Optional


def resolve_binary(name: str) -> str:
    if os.sep in name or (os.altsep and os.altsep in name) or name.endswith((".exe", ".cmd", ".bat")):
        return name
    found = shutil.which(name)
    if found:
        return found
    return name


class ProcChannel:

    def __init__(self, proc: subprocess.Popen, name: str = ""):
        self.proc = proc
        self.name = name
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._eof = threading.Event()
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name=f"stdout-{name or 'proc'}")
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            for line in self.proc.stdout:
                self._lines.put(line)
        except Exception:
            pass
        finally:
            self._lines.put(None)
            self._eof.set()

    def next_line(self, timeout: float = 0.2) -> Optional[str]:
        try:
            item = self._lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            return None
        return item

    def write(self, data: str) -> bool:
        with self._write_lock:
            stdin = self.proc.stdin
            if stdin is None or stdin.closed:
                return False
            try:
                stdin.write(data)
                stdin.flush()
                return True
            except Exception:
                return False

    def close_stdin(self) -> None:
        with self._write_lock:
            try:
                if self.proc.stdin and not self.proc.stdin.closed:
                    self.proc.stdin.close()
            except Exception:
                pass

    def poll(self) -> Optional[int]:
        return self.proc.poll()

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        self.close_stdin()
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def start_process(
    cmd: list[str],
    *,
    workdir: str | None = None,
    name: str = "",
    env: dict[str, str] | None = None,
) -> ProcChannel:
    resolved = [cmd[0]] + list(cmd[1:])
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.Popen(
        resolved,
        cwd=workdir,
        env=full_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    return ProcChannel(proc, name=name or resolved[0])
