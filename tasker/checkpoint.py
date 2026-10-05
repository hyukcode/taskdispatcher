from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .models import is_valid_id


class CheckpointConflict(RuntimeError):
    pass

CHECKPOINT_VERSION = 1

CHECKPOINT_STATUSES = frozenset({
    "started",
    "success",
    "failed",
})

@dataclass(frozen=True)
class AttemptCheckpoint:
    version: int
    session_id: str
    task_id: str
    execution_id: str
    attempt_id: str
    executor: str
    plan_signature: str
    status: str
    started_at: float
    ended_at: float | None = None
    exit_code: int | None = None
    error: str = ""

  

class CheckpointStore:
    """单进程内线程安全的检查点存储；不支持多个进程同时写同一会话。"""

    def __init__(self, base_dir: str | Path):
        self.base = Path(base_dir)
        self.base.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._lock = threading.RLock()

    def _path(self, session_id: str, task_id: str, attempt_id: str) -> Path:
        for value in (session_id, task_id, attempt_id):
            if not is_valid_id(value):
                raise ValueError(f"非法检查点 ID: {value!r}")
        return self.base / session_id / "checkpoints" / task_id / f"{attempt_id}.json"

    @staticmethod
    def _atomic_write(path: Path, checkpoint: AttemptCheckpoint) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=".checkpoint-", suffix=".tmp", delete=False,
            ) as file:
                temp_path = Path(file.name)
                json.dump(asdict(checkpoint), file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
            if os.name == "posix":
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def start(
        self, *, session_id: str, task_id: str,
        execution_id: str, attempt_id: str,
        executor: str, plan_signature: str,
    ) -> AttemptCheckpoint:
        path = self._path(session_id, task_id, attempt_id)
        if not is_valid_id(execution_id):
            raise ValueError("非法 execution_id")
        if not plan_signature:
            raise ValueError("缺少 plan_signature")
        checkpoint = AttemptCheckpoint(
            version=1, session_id=session_id, task_id=task_id,
            execution_id=execution_id, attempt_id=attempt_id,
            executor=executor, plan_signature=plan_signature,
            status="started", started_at=time.time(),
        )
        with self._lock:
            if path.exists():
                raise CheckpointConflict(f"检查点已存在: {attempt_id}")
            self._atomic_write(path, checkpoint)
        return checkpoint

    def load(
        self, session_id: str, task_id: str, attempt_id: str,
    ) -> AttemptCheckpoint | None:
        path = self._path(session_id, task_id, attempt_id)
        with self._lock:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        cp = AttemptCheckpoint(**data)
        if (cp.version != 1 or cp.status not in {"started", "success", "failed"}
            or (cp.session_id, cp.task_id, cp.attempt_id)
            != (session_id, task_id, attempt_id)):
            raise ValueError(f"损坏的检查点: {path}")
        return cp

    def finish(
        self, session_id: str, task_id: str, attempt_id: str,
        *, success: bool, exit_code: int | None = None, error: str = "",
    ) -> AttemptCheckpoint:
        with self._lock:
            current = self.load(session_id, task_id, attempt_id)
            if current is None:
                raise FileNotFoundError(attempt_id)
            target_status = "success" if success else "failed"
            if current.status != "started":
                if (current.status == target_status
                    and current.exit_code == exit_code
                    and current.error == error):
                    return current
                raise CheckpointConflict("检查点已完成，不能改写执行结果")
            updated = replace(
                current, status=target_status, ended_at=time.time(),
                exit_code=exit_code, error=error,
            )
            self._atomic_write(
                self._path(session_id, task_id, attempt_id), updated
            )
            return updated

    def list_for_task(self, session_id: str, task_id: str) -> list[AttemptCheckpoint]:
        """冷恢复时扫描某任务全部尝试；不假设 session.json 已保存 attempt_id。"""
        directory = self._path(session_id, task_id, "probe").parent
        if not directory.exists():
            return []
        with self._lock:
            paths = sorted(directory.glob("*.json"))
            result = [self.load(session_id, task_id, path.stem) for path in paths]
        return [cp for cp in result if cp is not None]

    def inspect_execution(
        self, session_id: str, task_id: str,
        execution_id: str, plan_signature: str,
    ) -> str:
        """给恢复协调器一个保守判定，而不是自动启动 Runner。"""
        attempts = [cp for cp in self.list_for_task(session_id, task_id)
                    if cp.execution_id == execution_id]
        if not attempts:
            return "NOT_FOUND"
        if any(cp.plan_signature != plan_signature for cp in attempts):
            return "PLAN_CHANGED"
        if any(cp.status == "started" for cp in attempts):
            return "RECOVERY_REQUIRED"
        if any(cp.status == "success" for cp in attempts):
            return "REUSE_RESULT"
        return "REVIEW_BEFORE_RETRY"

    def recovery_decision(
        self, session_id: str, task_id: str,
        attempt_id: str, plan_signature: str,
    ) -> str:
        cp = self.load(session_id, task_id, attempt_id)
        if cp is None:
            return "NOT_FOUND"
        if cp.plan_signature != plan_signature:
            return "PLAN_CHANGED"
        return {
            "started": "RECOVERY_REQUIRED",
            "success": "REUSE_RESULT",
            "failed": "REVIEW_BEFORE_RETRY",
        }[cp.status]
