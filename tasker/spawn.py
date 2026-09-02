
from __future__ import annotations

import os
import queue
import logging
import shutil
import subprocess
import threading
from collections import deque
from typing import Optional


logger = logging.getLogger(__name__)


def resolve_binary(name: str) -> str:
    if os.sep in name or (os.altsep and os.altsep in name) or name.endswith((".exe", ".cmd", ".bat")):
        return name
    found = shutil.which(name)
    if found:
        return found
    return name


class ProcChannel:

    def __init__(self, proc: subprocess.Popen, name: str = "", stderr_limit: int = 200):
        self.proc = proc
        self.name = name
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=max(1, stderr_limit))
        self._stderr_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name=f"stdout-{name or 'proc'}")
        self._stderr_reader = threading.Thread(
            target=self._stderr_loop,
            daemon=True,
            name=f"stderr-{name or 'proc'}",
        )
        self._reader.start()
        self._stderr_reader.start()

    def _read_loop(self) -> None:
        try:
            if self.proc.stdout is not None:
                for line in self.proc.stdout:
                    self._lines.put(line)
        except (OSError, ValueError) as exc:
            logger.debug("读取 %s stdout 失败: %s", self.name, exc, exc_info=True)
        finally:
            self._lines.put(None)

    def _stderr_loop(self) -> None:
        """持续消费 stderr，避免子进程因 stderr 缓冲区满而阻塞。"""
        try:
            if self.proc.stderr is not None:
                for line in self.proc.stderr:
                    with self._stderr_lock:
                        self._stderr_tail.append(line.rstrip("\r\n"))
        except (OSError, ValueError) as exc:
            logger.debug("读取 %s stderr 失败: %s", self.name, exc, exc_info=True)

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
            except (BrokenPipeError, OSError, ValueError) as exc:
                logger.debug("写入 %s stdin 失败: %s", self.name, exc, exc_info=True)
                return False

    def close_stdin(self) -> None:
        with self._write_lock:
            try:
                if self.proc.stdin and not self.proc.stdin.closed:
                    self.proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError) as exc:
                logger.debug("关闭 %s stdin 失败: %s", self.name, exc, exc_info=True)

    def poll(self) -> Optional[int]:
        return self.proc.poll()

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    @property
    def stderr_tail(self) -> str:
        """返回最近的 stderr，长度受 ``stderr_limit`` 限制。"""
        with self._stderr_lock:
            return "\n".join(self._stderr_tail)

    def stop(self) -> None:
        self.close_stdin()
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
        except (OSError, ValueError) as exc:
            logger.debug("终止 %s 失败: %s", self.name, exc, exc_info=True)
        try:
            self.proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("等待 %s 退出超时或失败，尝试 kill: %s", self.name, exc, exc_info=True)
            try:
                self.proc.kill()
            except (OSError, ValueError) as kill_exc:
                logger.debug("kill %s 失败: %s", self.name, kill_exc, exc_info=True)
            try:
                self.proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError) as wait_exc:
                logger.debug("等待 %s 被 kill 后退出失败: %s", self.name, wait_exc, exc_info=True)
        for reader in (self._reader, self._stderr_reader):
            if reader.is_alive() and reader is not threading.current_thread():
                reader.join(timeout=0.5)


def start_process(
    cmd: list[str],
    *,
    workdir: str | None = None,
    name: str = "",
    env: dict[str, str] | None = None,
) -> ProcChannel:
    if not cmd:
        raise ValueError("无法启动空的进程命令")
    resolved = list(cmd)
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
