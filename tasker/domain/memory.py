from __future__ import  annotations

import time

from dataclasses import dataclass
from enum import Enum 

class MemoryScope(str, Enum):
    SESSION = "session"
    LONG_TERM = "long_term"

class MemoryKind(str, Enum):
    TASK_RESULT = "task_result"
    FACT = "fact"
    DECISION = "decision"
    NOTE = "note"
    ARTIFACT_REF = "artifact_ref"

@dataclass(frozen=True)
class MemoryRecord:
    id: str
    scope: MemoryScope
    kind: MemoryKind
    content: str
    session_id: str = ""
    source_task_id: str = ""
    key: str = ""
    tags: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    priority: int = 50
    created_at: float = 0.0
    updated_at: float = 0.0

def new_memory_record(
    *,
    memory_id: str,
    scope: MemoryScope,
    kind: MemoryKind,
    content: str,
    session_id: str = "",
    source_task_id: str = "",
    key: str = "",
    tags: tuple[str, ...] = (),
    artifact_ids: tuple[str, ...] = (),
    priority: int = 50,
) -> MemoryRecord:
    now = time.time()
    return MemoryRecord(
        id=memory_id,
        scope=scope,
        kind=kind,
        content=content,
        session_id=session_id,
        source_task_id=source_task_id,
        key=key,
        tags=tags,
        artifact_ids=artifact_ids,
        priority=max(0, min(100, priority)),
        created_at=now,
        updated_at=now,
    )

def memory_record_to_dict(
    record: MemoryRecord,
) -> dict:
    return {
        "id": record.id,
        "scope": record.scope.value,
        "kind": record.kind.value,
        "content": record.content,
        "session_id": record.session_id,
        "source_task_id": record.source_task_id,
        "key": record.key,
        "tags": list(record.tags),
        "artifact_ids": list(record.artifact_ids),
        "priority": record.priority,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }

def memory_record_from_dict(data: dict) -> MemoryRecord:
    if not isinstance(data, dict):
        raise ValueError("memory record must be an object")
    raw_tags = data.get("tags", [])
    if not isinstance(raw_tags, list):
        raise ValueError("memory tags must be an array")
    tags = tuple(
        str(tag)
        for tag in raw_tags
        if str(tag).strip()
    )
    raw_artifact_ids = data.get("artifact_ids", []) or []
    if not isinstance(raw_artifact_ids, list):
        raise ValueError("memory artifact_ids must be an array")
    artifact_ids = tuple(
        str(value)
        for value
        in raw_artifact_ids
        if str(value).strip()
    )
    return MemoryRecord(
        id=str(data.get("id", "")),
        scope=MemoryScope(
            str(
                data.get("scope", MemoryScope.SESSION.value)
            )
        ),
        kind=MemoryKind(
            str(
                data.get("kind", MemoryKind.NOTE.value)
            )
        ),
        content=str(
            data.get("content", "") or ""
        ),
        session_id=str(
            data.get("session_id", "") or ""
        ),
        source_task_id=str(
            data.get("source_task_id", "") or ""
        ),
        key=str(
            data.get("key", "") or ""
        ),
        tags=tags,
        artifact_ids=artifact_ids,
        priority=max(
            0, min(100, int(data.get("priority", 50)))
        ),
        created_at=float(
            data.get("created_at", 0.0) or 0.0
        ),
        updated_at=float(
            data.get("updated_at", 0.0) or 0.0
        ),
    )