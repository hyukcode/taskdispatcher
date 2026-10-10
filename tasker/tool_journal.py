from __future__ import annotations

import json
import os
import tempfile
import threading
import time

from dataclasses import replace
from pathlib import Path

from .domain.tool_call import (
    TOOL_CALL_VERSION,
    ToolCallRecord,
    ToolCallStatus,
    ToolInvocation,
    ToolResult,
    record_from_dict,
    record_to_dict,
)

from .models import (
    is_valid_id,
)

# tool级执行事实

class ToolJournal:

    def __init__(
        self,
        session_base:
            str | Path,
        *,
        session_id: str,
    ) -> None:

        if not is_valid_id(
            session_id
        ):
            raise ValueError(
                "invalid session id"
            )

        self.session_id = (
            session_id
        )

        self.root = (
            Path(session_base)
            / session_id
            / "tool_calls"
        )

        self.root.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._lock = (
            threading.RLock()
        )

    def _path(
        self,
        invocation_id: str,
    ) -> Path:

        if not is_valid_id(
            invocation_id
        ):
            raise ValueError(
                "invalid invocation id"
            )

        return (
            self.root
            / f"{invocation_id}.json"
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
                prefix=".tool-",
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
                    default=str,
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

    def start(
        self,
        *,
        invocation: ToolInvocation,
        policy_action: str,
        policy_reason: str,
        side_effect: str,
        idempotency: str,
        status: ToolCallStatus = ToolCallStatus.REQUESTED,
    ) -> ToolCallRecord:

        now = time.time()

        record = ToolCallRecord(
            version=TOOL_CALL_VERSION,
            invocation=invocation,
            status=status,
            policy_action=(
                policy_action
            ),
            policy_reason=(
                policy_reason
            ),
            side_effect=(
                side_effect
            ),
            idempotency=(
                idempotency
            ),
            result=None,
            created_at=now,
            updated_at=now,
        )

        self.save(
            record
        )

        return record

    def save(
        self,
        record: ToolCallRecord,
    ) -> ToolCallRecord:
        with self._lock:
            self._atomic_write(
                self._path(
                    record
                    .invocation
                    .invocation_id
                ),
                record_to_dict(
                    record
                ),
            )
        return record

    def load(
        self,
        invocation_id: str,
    ) -> ToolCallRecord | None:

        path = self._path(
            invocation_id
        )

        with self._lock:
            if not path.exists():
                return None
            data = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )
        return record_from_dict(
            data
        )

    def transition(
        self,
        invocation_id: str,
        *,
        status: ToolCallStatus,
        result:
            ToolResult | None = None,
    ) -> ToolCallRecord:

        existing = self.load(
            invocation_id
        )
        if existing is None:
            raise KeyError(
                invocation_id
            )
        updated = replace(
            existing,
            status=status,
            result=result,
            updated_at=time.time(),
        )

        return self.save(
            updated
        )

    def list_all(
        self,
    ) -> list[ToolCallRecord]:

        records: list[
            ToolCallRecord
        ] = []

        for path in (
            self.root.glob(
                "*.json"
            )
        ):

            record = self.load(
                path.stem
            )
            if record is not None:
                records.append(
                    record
                )
        records.sort(
            key=lambda item:
                item.created_at
        )
        return records

    def find_success_by_idempotency(
        self,
        *,
        canonical_name: str,
        idempotency_key: str,
    ) -> ToolCallRecord | None:

        if not idempotency_key:
            return None

        for record in reversed(
            self.list_all()
        ):
            invocation = (
                record.invocation
            )
            if (
                invocation
                .canonical_name
                != canonical_name
            ):
                continue

            if (
                invocation
                .idempotency_key
                != idempotency_key
            ):
                continue

            if (
                record.status
                == ToolCallStatus.SUCCESS
                and record.result
                is not None
            ):
                return record

        return None

    def unresolved(
        self,
    ) -> list[ToolCallRecord]:

        return [
            record
            for record
            in self.list_all()
            if record.status
            in {
                ToolCallStatus
                .APPROVED,

                ToolCallStatus
                .RUNNING,

                ToolCallStatus
                .UNKNOWN,
            }
        ]