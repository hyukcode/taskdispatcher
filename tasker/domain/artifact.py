from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


ARTIFACT_VERSION = 1


class ArtifactKind(str, Enum):
    # Artifact 的业务类型
    TASK_OUTPUT = "task_output"
    TOOL_OUTPUT = "tool_output"
    REPORT = "report"
    LOG = "log"
    FILE = "file"


@dataclass(frozen=True)
class ArtifactRecord:
    # Artifact 元数据,只描述内容存放在哪里以及如何校验。
    version: int
    id: str
    session_id: str
    source_task_id: str
    kind: ArtifactKind
    name: str
    media_type: str
    relative_path: str
    size_bytes: int
    sha256: str
    created_at: float


def artifact_record_to_dict(
    record: ArtifactRecord,
) -> dict:
    return {
        "version": record.version,
        "id": record.id,
        "session_id": record.session_id,
        "source_task_id": record.source_task_id,
        "kind": record.kind.value,
        "name": record.name,
        "media_type": record.media_type,
        "relative_path": (
            record.relative_path
        ),
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "created_at": record.created_at,
    }


def artifact_record_from_dict(
    data: dict,
) -> ArtifactRecord:
    if not isinstance(data, dict):
        raise ValueError(
            "artifact metadata "
            "must be an object"
        )
    version = int(data.get("version", 0))
    if version != ARTIFACT_VERSION:
        raise ValueError(
            f"Unsupported artifact version: {version}"
        )
    record = ArtifactRecord(
        version=version,
        id=str(data.get("id","")),
        session_id=str(data.get("session_id","")),
        source_task_id=str(data.get("source_task_id","")),
        kind=ArtifactKind(str(data.get("kind",ArtifactKind.FILE.value))),
        name=str(data.get("name","")),
        media_type=str(data.get("media_type","text/plain")),
        relative_path=str(data.get("relative_path","")),
        size_bytes=int(data.get("size_bytes",0)),
        sha256=str(data.get("sha256","")),
        created_at=float(data.get("created_at",0.0)),
    )

    if not record.id:
        raise ValueError(
            "missing artifact id"
        )

    if not record.sha256:
        raise ValueError(
            "missing artifact sha256"
        )

    return record