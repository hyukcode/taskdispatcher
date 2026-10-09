from __future__ import annotations

import json
import os
import tempfile
import threading

from pathlib import Path

from .domain.memory import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    memory_record_from_dict,
    memory_record_to_dict,
)

from .models import is_valid_id


class MemoryStore:

    def __init__(
        self,
        session_base: str | Path,
        long_term_base: str | Path,
    ) -> None:

        self.session_base = Path(
            session_base
        )

        self.long_term_base = Path(
            long_term_base
        )

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
                f"invalid session_id: "
                f"{session_id!r}"
            )

        return (
            self.session_base
            / session_id
            / "memory"
        )

    def _path(
        self,
        *,
        scope: MemoryScope,
        memory_id: str,
        session_id: str = "",
    ) -> Path:

        if not is_valid_id(memory_id):
            raise ValueError(
                f"invalid memory_id: "
                f"{memory_id!r}"
            )

        if scope == MemoryScope.SESSION:

            if not session_id:
                raise ValueError(
                    "session_id required"
                )

            return (
                self._session_dir(
                    session_id
                )
                / f"{memory_id}.json"
            )

        return (
            self.long_term_base
            / f"{memory_id}.json"
        )

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

                temp_path = Path(
                    file.name
                )

                json.dump(
                    data,
                    file,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )

                file.flush()

                os.fsync(
                    file.fileno()
                )

            os.replace(
                temp_path,
                path,
            )

        finally:
            if (
                temp_path is not None
                and temp_path.exists()
            ):
                temp_path.unlink(
                    missing_ok=True
                )

    def save(
        self,
        record: MemoryRecord,
    ) -> MemoryRecord:

        path = self._path(
            scope=record.scope,
            memory_id=record.id,
            session_id=record.session_id,
        )

        with self._lock:
            self._atomic_write(
                path,
                memory_record_to_dict(
                    record
                ),
            )

        return record

    def load(
        self,
        *,
        scope: MemoryScope,
        memory_id: str,
        session_id: str = "",
    ) -> MemoryRecord | None:

        path = self._path(
            scope=scope,
            memory_id=memory_id,
            session_id=session_id,
        )

        with self._lock:

            if not path.exists():
                return None

            data = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )

        return memory_record_from_dict(
            data
        )

    def _list_directory(
        self,
        directory: Path,
        *,
        scope: MemoryScope,
        session_id: str = "",
        include_inactive: bool = False,
    ) -> list[MemoryRecord]:

        if not directory.exists():
            return []

        with self._lock:
            paths = list(
                directory.glob(
                    "*.json"
                )
            )

        records: list[
            MemoryRecord
        ] = []

        for path in paths:

            record = self.load(
                scope=scope,
                session_id=session_id,
                memory_id=path.stem,
            )

            if record is None:
                continue

            if (
                not include_inactive
                and record.status
                != MemoryStatus.ACTIVE
            ):
                continue

            records.append(record)

        records.sort(
            key=lambda record: (
                record.priority,
                record.updated_at,
            ),
            reverse=True,
        )

        return records

    def list_session(
        self,
        session_id: str,
        *,
        include_inactive: bool = False,
    ) -> list[MemoryRecord]:

        return self._list_directory(
            self._session_dir(
                session_id
            ),
            scope=MemoryScope.SESSION,
            session_id=session_id,
            include_inactive=(
                include_inactive
            ),
        )

    def list_long_term(
        self,
        *,
        include_inactive: bool = False,
    ) -> list[MemoryRecord]:

        return self._list_directory(
            self.long_term_base,
            scope=MemoryScope.LONG_TERM,
            include_inactive=(
                include_inactive
            ),
        )

    def delete(
        self,
        *,
        scope: MemoryScope,
        memory_id: str,
        session_id: str = "",
    ) -> bool:

        path = self._path(
            scope=scope,
            memory_id=memory_id,
            session_id=session_id,
        )

        with self._lock:

            if not path.exists():
                return False

            path.unlink()

        return True