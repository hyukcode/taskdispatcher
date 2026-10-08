from __future__ import annotations

import json
import os
import tempfile
import threading

from pathlib import Path

from .domain.memory import (
    MemoryRecord,
    MemoryScope,
    memory_record_from_dict,
    memory_record_to_dict,
)

from .models import is_valid_id

# memory 文件持久化
# session memory : sessions/<session_id>/memory/
# long-term memory : memory/long_term/
class MemoryStore:
    def __init__(
        self,
        session_base: str | Path,
        long_term_base: str | Path,
    ) -> None:
        self.session_base = Path(session_base)
        self.long_term_base = Path(long_term_base)
        self.session_base.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.long_term_base.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._lock = threading.RLock()
    
    def _session_dir(
        self,
        session_id: str,
    ) -> Path:
        if not is_valid_id(session_id):
            raise ValueError(
                f"invalid session_id: {session_id}"
            )
        return (
            self.session_base / session_id / "memory"
        )
    
    def _path(self, record: MemoryRecord) -> Path:
        if not is_valid_id(record.id):
            raise ValueError(f"invalid memory id: {record.id}")
        if record.scope == MemoryScope.SESSION:
            if not record.session_id:
                raise ValueError("session memory requires session_id")
            return self._session_dir(record.session_id) / f"{record.id}.json"
        if record.scope == MemoryScope.LONG_TERM:
            return self.long_term_base / f"{record.id}.json"
        raise ValueError(f"unsupported scope: {record.scope}")

    @staticmethod
    def _atomic_write(
        path: Path,
        data: dict,
    ) -> None:
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".memory-",
                suffix=".tmp",
                delete=False,
            ) as file:
                temp_path = Path(file.name)
                json.dump(
                    data,
                    file,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)
    
    def save(
        self,
        record: MemoryRecord,
    ) -> MemoryRecord:
        path = self._path(record)
        with self._lock:
            self._atomic_write(
                path,
                memory_record_to_dict(record),
            )

        return record
    
    def load(
        self,
        *,
        scope: MemoryScope,
        memory_id: str,
        session_id: str = "",
    ) -> MemoryRecord | None:
        if not is_valid_id(memory_id):
            raise ValueError(f"invalid memory id : {memory_id}")

        if scope == MemoryScope.SESSION:
            if not session_id:
                raise ValueError("session_id is required")
            path = self._session_dir(session_id) / f"{memory_id}.json"
        else:
            path = self.long_term_base / f"{memory_id}.json"
        
        with self._lock:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        return memory_record_from_dict(data)

    def list_session(
        self,
        session_id: str,
    ) -> list[MemoryRecord]:
        directory = self._session_dir(session_id)
        if not directory.exists():
            return []
        with self._lock:
            paths = list(directory.glob("*.json"))
        result = []
        for path in paths:
            record = self.load(
                scope=MemoryScope.SESSION,
                session_id=session_id,
                memory_id=path.stem,
            )
            if record is not None:
                result.append(record)
        result.sort(
            key=lambda record: (
                record.priority,
                record.updated_at,
            ),
            reverse=True,
        )
        return result

    def list_long_term(
        self,
    ) -> list[MemoryRecord]:
        if not self.long_term_base.exists():
            return []
        with self._lock:
            paths = list(
                self.long_term_base.glob("*.json")
            )
        result = []
        for path in paths:
            record = self.load(
                scope=MemoryScope.LONG_TERM,
                memory_id=path.stem,
            )
            if record is not None:
                result.append(record)
        result.sort(
            key=lambda record: (
                record.priority,
                record.updated_at,
            ),
            reverse=True,
        )
        return result

