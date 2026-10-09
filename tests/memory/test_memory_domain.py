from dataclasses import replace

import pytest

from tasker.domain.memory import (
    MEMORY_VERSION,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    memory_record_from_dict,
    memory_record_to_dict,
    new_memory_record,
)


def test_new_memory_record_defaults():
    record = new_memory_record(
        memory_id="m-test",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.FACT,
        content="数据库使用 MySQL",
        session_id="s1",
        key="database",
    )

    assert record.version == MEMORY_VERSION

    assert (
        record.scope
        == MemoryScope.SESSION
    )

    assert (
        record.kind
        == MemoryKind.FACT
    )

    assert (
        record.status
        == MemoryStatus.ACTIVE
    )

    assert record.content == (
        "数据库使用 MySQL"
    )

    assert record.session_id == "s1"

    assert record.key == "database"

    assert record.priority == 50

    assert record.created_at > 0
    assert record.updated_at > 0


def test_priority_is_clamped():
    high = new_memory_record(
        memory_id="m-high",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.NOTE,
        content="high",
        session_id="s1",
        priority=999,
    )

    low = new_memory_record(
        memory_id="m-low",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.NOTE,
        content="low",
        session_id="s1",
        priority=-100,
    )

    assert high.priority == 100
    assert low.priority == 0


def test_memory_round_trip():
    original = new_memory_record(
        memory_id="m-roundtrip",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.DECISION,
        content="数据库改为 MySQL",
        session_id="s1",
        source_task_id="t1",
        key="database",
        tags=(
            "database",
            "mysql",
        ),
        artifact_ids=(
            "a-one",
            "a-two",
        ),
        priority=90,
    )

    data = memory_record_to_dict(
        original
    )

    restored = (
        memory_record_from_dict(
            data
        )
    )

    assert restored == original


def test_old_memory_without_status_defaults_active():
    record = new_memory_record(
        memory_id="m-old",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.FACT,
        content="old",
        session_id="s1",
    )

    data = memory_record_to_dict(
        record
    )

    data.pop("status")

    restored = (
        memory_record_from_dict(
            data
        )
    )

    assert (
        restored.status
        == MemoryStatus.ACTIVE
    )


def test_inactive_memory_round_trip():
    record = new_memory_record(
        memory_id="m-old",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.DECISION,
        content="PostgreSQL",
        session_id="s1",
        key="database",
    )

    record = replace(
        record,
        status=(
            MemoryStatus.SUPERSEDED
        ),
        superseded_by="m-new",
    )

    restored = (
        memory_record_from_dict(
            memory_record_to_dict(
                record
            )
        )
    )

    assert (
        restored.status
        == MemoryStatus.SUPERSEDED
    )

    assert (
        restored.superseded_by
        == "m-new"
    )


def test_invalid_memory_root_rejected():
    with pytest.raises(
        ValueError
    ):
        memory_record_from_dict(
            []
        )


def test_invalid_memory_version_rejected():
    record = new_memory_record(
        memory_id="m-version",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.NOTE,
        content="test",
        session_id="s1",
    )

    data = memory_record_to_dict(
        record
    )

    data["version"] = 999

    with pytest.raises(
        ValueError
    ):
        memory_record_from_dict(
            data
        )