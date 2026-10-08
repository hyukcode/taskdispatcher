from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time

from pathlib import Path

from .domain.artifact import (
    ARTIFACT_VERSION,
    ArtifactKind,
    ArtifactRecord,
    artifact_record_from_dict,
    artifact_record_to_dict,
)

from .models import is_valid_id


class ArtifactError(RuntimeError):
    pass


class ArtifactCorrupted(
    ArtifactError
):
    pass


class ArtifactStore:
    """
    Session Workspace 内的 Artifact 存储。
    workspace/<session_id>/artifacts/<task_id>/<artifact_id>/
                        metadata.json
                        payload.txt
    """

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        session_id: str,
    ) -> None:

        if not is_valid_id(session_id):
            raise ValueError(
                f"invalid session_id: "
                f"{session_id}"
            )

        self.workspace_root = Path(
            workspace_root
        )

        self.session_id = session_id

        self.root = (
            self.workspace_root
            / "artifacts"
        )

        self.root.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._lock = threading.RLock()

    @staticmethod
    def _validate_id(
        name: str,
        value: str,
    ) -> None:

        if not is_valid_id(value):
            raise ValueError(
                f"invalid {name}: "
                f"{value}"
            )

    def _artifact_dir(
        self,
        task_id: str,
        artifact_id: str,
    ) -> Path:

        self._validate_id(
            "task_id",
            task_id,
        )

        self._validate_id(
            "artifact_id",
            artifact_id,
        )

        return (
            self.root
            / task_id
            / artifact_id
        )

    def _metadata_path(
        self,
        task_id: str,
        artifact_id: str,
    ) -> Path:

        return (
            self._artifact_dir(
                task_id,
                artifact_id,
            )
            / "metadata.json"
        )

    def _payload_path(
        self,
        task_id: str,
        artifact_id: str,
    ) -> Path:

        return (
            self._artifact_dir(
                task_id,
                artifact_id,
            )
            / "payload.txt"
        )

    @staticmethod
    def _atomic_write_bytes(
        path: Path,
        payload: bytes,
    ) -> None:

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temp_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=".artifact-",
                suffix=".tmp",
                delete=False,
            ) as file:
                temp_path = Path(file.name)
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())

            os.replace(
                temp_path,
                path,
            )

        finally:
            if (
                temp_path is not None
                and temp_path.exists()
            ):
                temp_path.unlink(missing_ok=True)

    @classmethod
    def _atomic_write_json(
        cls,
        path: Path,
        data: dict,
    ) -> None:

        payload = json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

        cls._atomic_write_bytes(
            path,
            payload,
        )

    def _artifact_id(
        self,
        *,
        task_id: str,
        kind: ArtifactKind,
        name: str,
        payload_sha256: str,
    ) -> str:

        raw = (
            f"{self.session_id}|"
            f"{task_id}|"
            f"{kind.value}|"
            f"{name}|"
            f"{payload_sha256}"
        )

        digest = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()[:24]

        return f"a-{digest}"

    def save_text(
        self,
        *,
        task_id: str,
        name: str,
        content: str,
        kind: ArtifactKind = (
            ArtifactKind.TASK_OUTPUT
        ),
        media_type: str = (
            "text/plain; charset=utf-8"
        ),
    ) -> ArtifactRecord:

        self._validate_id("task_id",task_id)

        payload = content.encode("utf-8")

        payload_sha256 = hashlib.sha256(payload).hexdigest()

        artifact_id = self._artifact_id(
            task_id=task_id,
            kind=kind,
            name=name,
            payload_sha256=payload_sha256,
        )

        directory = self._artifact_dir(task_id,artifact_id)

        relative_path = (
            Path("artifacts")
            / task_id
            / artifact_id
            / "payload.txt"
        ).as_posix()

        record = ArtifactRecord(
            version=ARTIFACT_VERSION,
            id=artifact_id,
            session_id=self.session_id,
            source_task_id=task_id,
            kind=kind,
            name=name,
            media_type=media_type,
            relative_path=(
                relative_path
            ),
            size_bytes=len(payload),
            sha256=payload_sha256,
            created_at=time.time(),
        )

        metadata_path = (
            directory
            / "metadata.json"
        )

        payload_path = (
            directory
            / "payload.txt"
        )

        with self._lock:
            # 相同内容产生相同 ID，已经存在则直接复用。
            if metadata_path.exists():
                existing = self.load(
                    task_id=task_id,
                    artifact_id=(
                        artifact_id
                    ),
                )

                if existing is None:
                    raise ArtifactCorrupted(
                        "artifact metadata "
                        "disappeared"
                    )

                if (
                    existing.sha256
                    != payload_sha256
                ):
                    raise ArtifactCorrupted(
                        "artifact id collision"
                    )

                return existing

            # 注意顺序：
            # 1. 先写 payload
            # 2. 最后写 metadata
            # metadata 相当于 commit marker
            self._atomic_write_bytes(
                payload_path,
                payload,
            )

            self._atomic_write_json(
                metadata_path,
                artifact_record_to_dict(
                    record
                ),
            )

        return record

    def load(
        self,
        *,
        task_id: str,
        artifact_id: str,
    ) -> ArtifactRecord | None:

        metadata_path = (
            self._metadata_path(
                task_id,
                artifact_id,
            )
        )

        with self._lock:
            if not metadata_path.exists():
                return None
            try:
                data = json.loads(
                    metadata_path.read_text(
                        encoding="utf-8"
                    )
                )
            except (
                OSError,
                json.JSONDecodeError,
            ) as exc:
                raise ArtifactCorrupted(
                    "invalid artifact metadata"
                ) from exc

        record = (
            artifact_record_from_dict(
                data
            )
        )

        if (
            record.session_id
            != self.session_id
        ):
            raise ArtifactCorrupted(
                "artifact session mismatch"
            )

        if (
            record.source_task_id
            != task_id
        ):
            raise ArtifactCorrupted(
                "artifact task mismatch"
            )

        if (
            record.id
            != artifact_id
        ):
            raise ArtifactCorrupted(
                "artifact id mismatch"
            )

        return record

    def read_text(
        self,
        record: ArtifactRecord,
        *,
        max_chars: int | None = None,
    ) -> str:

        if (
            record.session_id
            != self.session_id
        ):
            raise ArtifactCorrupted(
                "artifact session mismatch"
            )

        path = self._payload_path(
            record.source_task_id,
            record.id,
        )

        try:
            payload = path.read_bytes()

        except OSError as exc:
            raise ArtifactCorrupted(
                "artifact payload missing"
            ) from exc

        actual_sha256 = (
            hashlib.sha256(
                payload
            ).hexdigest()
        )

        if (
            actual_sha256
            != record.sha256
        ):
            raise ArtifactCorrupted(
                "artifact checksum mismatch"
            )

        if (
            len(payload)
            != record.size_bytes
        ):
            raise ArtifactCorrupted(
                "artifact size mismatch"
            )

        try:
            content = payload.decode(
                "utf-8"
            )

        except UnicodeDecodeError as exc:
            raise ArtifactCorrupted(
                "artifact is not utf-8 text"
            ) from exc

        if (
            max_chars is not None
            and len(content) > max_chars
        ):
            return (
                content[:max_chars]
                + "\n...[Artifact 截断]..."
            )

        return content

    def list_for_task(
        self,
        task_id: str,
    ) -> list[ArtifactRecord]:

        self._validate_id("task_id",task_id)

        task_dir = self.root / task_id

        if not task_dir.exists():
            return []

        records: list[ArtifactRecord] = []

        with self._lock:
            directories = [
                path
                for path
                in task_dir.iterdir()
                if path.is_dir()
            ]

        for directory in directories:
            record = self.load(
                task_id=task_id,
                artifact_id=directory.name,
            )
            if record is not None:
                records.append(record)

        records.sort(
            key=lambda record:
                record.created_at
        )
        return records