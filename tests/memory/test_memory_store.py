from dataclasses import replace

from tasker.domain.memory import (
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    new_memory_record,
)

from tasker.memory_store import (
    MemoryStore,
)


def make_store(tmp_path):
    return MemoryStore(
        session_base=(
            tmp_path
            / "sessions"
        ),
        long_term_base=(
            tmp_path
            / "long_term"
        ),
    )


def test_save_and_load_session_memory(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    record = new_memory_record(
        memory_id="m-one",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.FACT,
        content="数据库使用 MySQL",
        session_id="s1",
        key="database",
    )

    store.save(record)

    loaded = store.load(
        scope=MemoryScope.SESSION,
        session_id="s1",
        memory_id="m-one",
    )

    assert loaded == record


def test_save_and_load_long_term_memory(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    record = new_memory_record(
        memory_id="m-global",
        scope=(
            MemoryScope.LONG_TERM
        ),
        kind=MemoryKind.DECISION,
        content="项目使用 FastAPI",
        key="backend-framework",
    )

    store.save(record)

    loaded = store.load(
        scope=(
            MemoryScope.LONG_TERM
        ),
        memory_id="m-global",
    )

    assert loaded == record


def test_missing_memory_returns_none(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    assert (
        store.load(
            scope=(
                MemoryScope.SESSION
            ),
            session_id="s1",
            memory_id="m-missing",
        )
        is None
    )


def test_list_session_only_returns_active_by_default(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    active = new_memory_record(
        memory_id="m-active",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.FACT,
        content="active",
        session_id="s1",
    )

    archived = new_memory_record(
        memory_id="m-archived",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.NOTE,
        content="archived",
        session_id="s1",
    )

    archived = replace(
        archived,
        status=(
            MemoryStatus.ARCHIVED
        ),
    )

    store.save(active)
    store.save(archived)

    records = store.list_session(
        "s1"
    )

    assert [
        record.id
        for record in records
    ] == [
        "m-active"
    ]


def test_list_session_can_include_inactive(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    active = new_memory_record(
        memory_id="m-active",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.FACT,
        content="active",
        session_id="s1",
    )

    old = new_memory_record(
        memory_id="m-old",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.FACT,
        content="old",
        session_id="s1",
    )

    old = replace(
        old,
        status=(
            MemoryStatus.SUPERSEDED
        ),
    )

    store.save(active)
    store.save(old)

    records = store.list_session(
        "s1",
        include_inactive=True,
    )

    ids = {
        record.id
        for record in records
    }

    assert ids == {
        "m-active",
        "m-old",
    }


def test_memory_sorted_by_priority_then_time(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    low = new_memory_record(
        memory_id="m-low",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.FACT,
        content="low",
        session_id="s1",
        priority=20,
    )

    high = new_memory_record(
        memory_id="m-high",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.FACT,
        content="high",
        session_id="s1",
        priority=90,
    )

    store.save(low)
    store.save(high)

    records = store.list_session(
        "s1"
    )

    assert records[0].id == (
        "m-high"
    )


def test_delete_memory(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    record = new_memory_record(
        memory_id="m-delete",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.NOTE,
        content="delete me",
        session_id="s1",
    )

    store.save(record)

    assert store.delete(
        scope=MemoryScope.SESSION,
        session_id="s1",
        memory_id="m-delete",
    )

    assert (
        store.load(
            scope=MemoryScope.SESSION,
            session_id="s1",
            memory_id="m-delete",
        )
        is None
    )

    assert not store.delete(
        scope=MemoryScope.SESSION,
        session_id="s1",
        memory_id="m-delete",
    )