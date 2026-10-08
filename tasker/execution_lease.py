# 多终端同时resume同一session，只允许一个进程真正执行

from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import time
import uuid

from dataclasses import asdict, dataclass, replace

from pathlib import Path 
from typing import Callable

LEASE_VERSION = 1

@dataclass(frozen=True)
class LeaseRecord:
    version: int
    session_id: str
    owner_id: str
    pid: int
    hostname: str
    acquired_at: float
    heartbeat_at: float

class LeaseHeldError(RuntimeError):
    def __init__(
        self,
        record: LeaseRecord | None,
    ) -> None:
        self.record = record
        if record is None:
            message = (
                "session execution lease "
                "is already held"
            )
        else:
            message = (
                "session execution lease "
                f"is held by "
                f"{record.owner_id} "
                f"(pid={record.pid}, "
                f"host={record.hostname})"
            )
        super().__init__(message)
    

class LeaseHostError(RuntimeError):
    pass

class ExecutionLease:
    def __init__(
        self,
        session_dir: str | Path,
        *,
        session_id: str,
        ttl_seconds: float = 30.0,
        heartbeat_seconds: float = 5.0,
        owner_id: str | None = None,
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(
                "ttl_seconds must be > 0"
            )

        if heartbeat_seconds <= 0:
            raise ValueError(
                "heartbeat_seconds must be > 0"
            )

        if heartbeat_seconds >= ttl_seconds:
            raise ValueError(
                "heartbeat_seconds must be "
                "smaller than ttl_seconds"
            )
        
        self.session_dir = Path(
            session_dir
        )

        self.session_id = session_id

        self.ttl_seconds = float(ttl_seconds)

        self.heartbeat_seconds = float(heartbeat_seconds)

        self.owner_id = owner_id or (
            f"{os.getpid()}-"
            f"{uuid.uuid4().hex[:12]}"
        )

        self.on_lost = on_lost

        self.lease_dir = (
            self.session_dir
            / ".execution-lease"
        )

        self.record_path = (
            self.lease_dir
            / "lease.json"
        )

        # 线程同步
        self._stop = threading.Event()

        self._lost = threading.Event()

        self._lock = threading.RLock()

        self._thread: threading.Thread | None = None

        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired
    
    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def _read_record(
        self,
    ) -> LeaseRecord | None:
        try:
            data = json.loads(
                self.record_path.read_text(
                    encoding="utf-8"
                )
            )
            return LeaseRecord(**data)
        except (
            FileNotFoundError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            OSError
        ):
            return None
        
    def _write_record(
        self,
        record: LeaseRecord,
    ) -> None:
        self.lease_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        temp_path = self.lease_dir / (
            f".lease-"
            f"{self.owner_id}.tmp"
        )
        try:
            with open(
                temp_path,
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    asdict(record),
                    file,
                    ensure_ascii=False,
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(
                temp_path,
                self.record_path,
            )
        finally:
            temp_path.unlink(
                missing_ok=True
            )

    def _is_stale(
        self,
        record: LeaseRecord | None,
    ) -> bool:
        now = time.time()
        if record is not None:
            return (
                now - record.heartbeat_at > self.ttl_seconds
            )
        try:
            modified = (
                self.lease_dir.stat().st_mtime
            )
        except FileNotFoundError:
            return True
        
        return (
            now
            - modified
            > self.ttl_seconds
        )

    def acquire(
        self,
    ) -> LeaseRecord:
        self.session_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        for _ in range(8):

            try:
                # 跨进程互斥
                self.lease_dir.mkdir()

            except FileExistsError:
                current = (
                    self._read_record()
                )
                if not self._is_stale(
                    current
                ):
                    raise LeaseHeldError(
                        current
                    )
                stale_dir = (
                    self.session_dir
                    / (
                        ".execution-lease.stale-"
                        + uuid.uuid4().hex
                    )
                )
                try:
                    os.rename(
                        self.lease_dir,
                        stale_dir,
                    )
                except FileNotFoundError:
                    continue
                except OSError:
                    current = (
                        self._read_record()
                    )
                    if not self._is_stale(
                        current
                    ):
                        raise LeaseHeldError(
                            current
                        )
                    raise

                shutil.rmtree(
                    stale_dir,
                    ignore_errors=True,
                )
                continue

            now = time.time()

            record = LeaseRecord(
                version=LEASE_VERSION,
                session_id=self.session_id,
                owner_id=self.owner_id,
                pid=os.getpid(),
                hostname=socket.gethostname(),
                acquired_at=now,
                heartbeat_at=now,
            )

            try:
                self._write_record(record)
            except Exception:
                shutil.rmtree(
                    self.lease_dir,
                    ignore_errors=True,
                )
                raise
            self._acquired = True
            self._start_heartbeat()
            return record
        raise RuntimeError("unable to acquire execution lease")
        

    def refresh(self) -> LeaseRecord:
        with self._lock:
            if not self._acquired:
                 raise LeaseHostError("lease is not acquired")
            current = self._read_record()
            if current is None or current.owner_id != self.owner_id:
                self._mark_lost()
                raise LeaseHostError(
                    "execution lease ownership "
                    "was lost"
                )

            updated = replace(
                current,
                heartbeat_at=time.time(),
            )

            self._write_record(
                updated
            )

            return updated

    def _mark_lost(
        self,
    ) -> None:
        if self._lost.is_set():
            return
        self._lost.set()
        callback = self.on_lost
        if callback is not None:
            try:
                callback()
            except Exception:
                pass
    
    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                self.refresh()
            except Exception:
                self._mark_lost()
                return

    def _start_heartbeat(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"session-lease-{self.session_id}",
            daemon=True,
        )
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.heartbeat_seconds+1.0)
        with self._lock:
            if not self._acquired:
                return
            current = self._read_record()
            if current is not None and current.owner_id == self.owner_id:
                released_dir = self.session_dir / (".execution-lease.release-" + uuid.uuid4().hex)
                try:
                    os.rename(self.lease_dir, released_dir)
                except FileNotFoundError:
                    pass
                else:
                    shutil.rmtree(released_dir, ignore_errors=True)
            self._acquired=False
    
    def __enter__(self) -> "ExecutionLease":
        self.acquire()
        return self
    
    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()




