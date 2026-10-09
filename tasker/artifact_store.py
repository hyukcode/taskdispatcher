from __future__ import annotations

import hashlib
import json
import os
import shutil
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

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        session_id: str,
    ) -> None:

        if not is_valid_id(session_id):
            raise ValueError(
                f"invalid session_id: "
                f"{session_id!r}"
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

    def _artifact_dir(
        self,
        task_id: str,
        artifact_id: str,
    ) -> Path:

        if not is_valid_id(task_id):
            raise ValueError(
                f"invalid task_id: "
                f"{task_id!r}"
            )

        if not is_valid_id(artifact_id):
            raise ValueError(
                f"invalid artifact_id: "
                f"{artifact_id!r}"
            )

        return (
            self.root
            / task_id
            / artifact_id
        )

    @staticmethod
    def _atomic_write(
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

                temp_path = Path(
                    file.name
                )

                file.write(payload)

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

    def _artifact_id(
        self,
        *,
        task_id: str,
        kind: ArtifactKind,
        name: str,
        sha256: str,
    ) -> str:

        raw = (
            f"{self.session_id}|"
            f"{task_id}|"
            f"{kind.value}|"
            f"{name}|"
            f"{sha256}"
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

        payload = content.encode(
            "utf-8"
        )

        sha256 = hashlib.sha256(
            payload
        ).hexdigest()

        artifact_id = (
            self._artifact_id(
                task_id=task_id,
                kind=kind,
                name=name,
                sha256=sha256,
            )
        )

        directory = (
            self._artifact_dir(
                task_id,
                artifact_id,
            )
        )

        payload_path = (
            directory
            / "payload.txt"
        )

        metadata_path = (
            directory
            / "metadata.json"
        )

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
            sha256=sha256,

            created_at=time.time(),
        )

        with self._lock:

            if metadata_path.exists():

                existing = self.load(
                    task_id=task_id,
                    artifact_id=artifact_id,
                )

                if existing is None:
                    raise ArtifactCorrupted(
                        "artifact disappeared"
                    )

                return existing

            # payload first
            self._atomic_write(
                payload_path,
                payload,
            )

            # metadata acts as commit marker
            metadata_payload = json.dumps(
                artifact_record_to_dict(
                    record
                ),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")

            self._atomic_write(
                metadata_path,
                metadata_payload,
            )

        return record

    def load(
        self,
        *,
        task_id: str,
        artifact_id: str,
    ) -> ArtifactRecord | None:

        directory = self._artifact_dir(
            task_id,
            artifact_id,
        )

        metadata_path = (
            directory
            / "metadata.json"
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
                "session mismatch"
            )

        if (
            record.source_task_id
            != task_id
        ):
            raise ArtifactCorrupted(
                "task mismatch"
            )

        if record.id != artifact_id:
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

        directory = self._artifact_dir(
            record.source_task_id,
            record.id,
        )

        payload_path = (
            directory
            / "payload.txt"
        )

        try:
            payload = (
                payload_path.read_bytes()
            )

        except OSError as exc:
            raise ArtifactCorrupted(
                "payload missing"
            ) from exc

        actual_sha = hashlib.sha256(
            payload
        ).hexdigest()

        if actual_sha != record.sha256:
            raise ArtifactCorrupted(
                "checksum mismatch"
            )

        if (
            len(payload)
            != record.size_bytes
        ):
            raise ArtifactCorrupted(
                "size mismatch"
            )

        content = payload.decode(
            "utf-8"
        )

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

        task_dir = (
            self.root
            / task_id
        )

        if not task_dir.exists():
            return []

        records: list[
            ArtifactRecord
        ] = []

        for directory in (
            task_dir.iterdir()
        ):

            if not directory.is_dir():
                continue

            record = self.load(
                task_id=task_id,
                artifact_id=(
                    directory.name
                ),
            )

            if record is not None:
                records.append(record)

        records.sort(
            key=lambda item:
                item.created_at
        )

        return records

    def list_all(
        self,
    ) -> list[ArtifactRecord]:

        if not self.root.exists():
            return []

        records: list[
            ArtifactRecord
        ] = []

        for task_dir in self.root.iterdir():

            if not task_dir.is_dir():
                continue

            records.extend(
                self.list_for_task(
                    task_dir.name
                )
            )

        return records

    def delete(
        self,
        record: ArtifactRecord,
    ) -> bool:

        directory = self._artifact_dir(
            record.source_task_id,
            record.id,
        )

        with self._lock:

            if not directory.exists():
                return False

            shutil.rmtree(
                directory
            )

        return True