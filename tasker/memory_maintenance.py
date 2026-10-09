from __future__ import annotations

import time

from dataclasses import replace

from .artifact_store import (
    ArtifactStore,
)

from .domain.memory import (
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
)

from .memory_store import (
    MemoryStore,
)


class MemoryMaintenanceService:

    def __init__(
        self,
        memory_store: MemoryStore,
        *,
        session_id: str,

        artifact_store:
            ArtifactStore | None = None,
    ) -> None:

        self.memory_store = (
            memory_store
        )

        self.artifact_store = (
            artifact_store
        )

        self.session_id = session_id

    # =========================
    # consolidate duplicates
    # =========================

    def consolidate(
        self,
        *,
        max_task_results: int = 20,
        max_notes: int = 20,
    ) -> dict[str, int]:

        records = (
            self.memory_store
            .list_session(
                self.session_id,
                include_inactive=True,
            )
        )

        superseded = (
            self._supersede_duplicate_keys(
                records
            )
        )

        # 重新读取，
        # 因为上一阶段可能改变状态。
        active = (
            self.memory_store
            .list_session(
                self.session_id
            )
        )

        archived = 0

        archived += (
            self._archive_excess(
                active,
                kind=(
                    MemoryKind.TASK_RESULT
                ),
                keep=max_task_results,
            )
        )

        # 再读取一次，
        # 防止同一列表状态陈旧。
        active = (
            self.memory_store
            .list_session(
                self.session_id
            )
        )

        archived += (
            self._archive_excess(
                active,
                kind=MemoryKind.NOTE,
                keep=max_notes,
            )
        )

        return {
            "superseded": superseded,
            "archived": archived,
        }

    def _supersede_duplicate_keys(
        self,
        records: list[
            MemoryRecord
        ],
    ) -> int:

        groups: dict[
            tuple[
                MemoryKind,
                str,
            ],
            list[
                MemoryRecord
            ],
        ] = {}

        for record in records:

            if (
                record.status
                != MemoryStatus.ACTIVE
            ):
                continue

            if not record.key:
                continue

            identity = (
                record.kind,
                record.key,
            )

            groups.setdefault(
                identity,
                [],
            ).append(record)

        changed = 0

        for group in groups.values():

            if len(group) <= 1:
                continue

            ordered = sorted(
                group,
                key=lambda record:
                    record.updated_at,
                reverse=True,
            )

            winner = ordered[0]

            for obsolete in ordered[1:]:

                updated = replace(
                    obsolete,

                    status=(
                        MemoryStatus
                        .SUPERSEDED
                    ),

                    superseded_by=(
                        winner.id
                    ),

                    updated_at=time.time(),
                )

                self.memory_store.save(
                    updated
                )

                changed += 1

        return changed

    def _archive_excess(
        self,
        records: list[
            MemoryRecord
        ],
        *,
        kind: MemoryKind,
        keep: int,
    ) -> int:

        if keep < 0:
            keep = 0

        candidates = [
            record
            for record in records
            if (
                record.status
                == MemoryStatus.ACTIVE
                and record.kind == kind
            )
        ]

        candidates.sort(
            key=lambda record:
                record.updated_at,
            reverse=True,
        )

        changed = 0

        for record in (
            candidates[keep:]
        ):

            archived = replace(
                record,

                status=(
                    MemoryStatus.ARCHIVED
                ),

                updated_at=time.time(),
            )

            self.memory_store.save(
                archived
            )

            changed += 1

        return changed

    # =========================
    # artifact gc
    # =========================

    def gc_artifacts(
        self,
        *,
        grace_seconds: float = 3600,
    ) -> int:

        if self.artifact_store is None:
            return 0

        records = (
            self.memory_store
            .list_session(
                self.session_id
            )
        )

        referenced = {
            artifact_id
            for record in records
            for artifact_id
            in record.artifact_ids
        }

        now = time.time()

        deleted = 0

        for artifact in (
            self.artifact_store
            .list_all()
        ):

            if artifact.id in referenced:
                continue

            age = (
                now
                - artifact.created_at
            )

            if age < grace_seconds:
                continue

            if self.artifact_store.delete(
                artifact
            ):
                deleted += 1

        return deleted

    def run(
        self,
        *,
        max_task_results: int = 20,
        max_notes: int = 20,
        artifact_grace_seconds:
            float = 3600,
    ) -> dict[str, int]:

        result = self.consolidate(
            max_task_results=(
                max_task_results
            ),
            max_notes=max_notes,
        )

        result["artifacts_deleted"] = (
            self.gc_artifacts(
                grace_seconds=(
                    artifact_grace_seconds
                )
            )
        )

        return result