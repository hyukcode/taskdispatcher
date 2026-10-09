from dataclasses import replace

from tasker.artifact_store import (
    ArtifactStore,
)

from tasker.domain.memory import (
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    new_memory_record,
)

from tasker.memory_maintenance import (
    MemoryMaintenanceService,
)

from tasker.memory_store import (
    MemoryStore,
)


def make_stores(tmp_path):
    memory_store = MemoryStore(
        session_base=(
            tmp_path / "sessions"
        ),
        long_term_base=(
            tmp_path / "long"
        ),
    )

    artifact_store = ArtifactStore(
        workspace_root=(
            tmp_path
            / "workspace"
            / "s1"
        ),
        session_id="s1",
    )

    return (
        memory_store,
        artifact_store,
    )


def test_duplicate_keys_are_superseded(
    tmp_path,
):
    store, artifact_store = (
        make_stores(
            tmp_path
        )
    )

    old = new_memory_record(
        memory_id="m-old",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.DECISION,
        content="PostgreSQL",
        session_id="s1",
        key="database",
    )

    old = replace(
        old,
        updated_at=1.0,
    )

    new = new_memory_record(
        memory_id="m-new",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.DECISION,
        content="MySQL",
        session_id="s1",
        key="database",
    )

    new = replace(
        new,
        updated_at=2.0,
    )

    store.save(old)
    store.save(new)

    service = (
        MemoryMaintenanceService(
            store,
            session_id="s1",
            artifact_store=(
                artifact_store
            ),
        )
    )

    result = service.consolidate(
        max_task_results=20,
        max_notes=20,
    )

    assert (
        result["superseded"]
        == 1
    )

    old_after = store.load(
        scope=MemoryScope.SESSION,
        session_id="s1",
        memory_id="m-old",
    )

    new_after = store.load(
        scope=MemoryScope.SESSION,
        session_id="s1",
        memory_id="m-new",
    )

    assert (
        old_after.status
        == MemoryStatus.SUPERSEDED
    )

    assert (
        old_after.superseded_by
        == "m-new"
    )

    assert (
        new_after.status
        == MemoryStatus.ACTIVE
    )


def test_excess_task_results_are_archived(
    tmp_path,
):
    store, artifact_store = (
        make_stores(
            tmp_path
        )
    )

    for index in range(4):

        record = new_memory_record(
            memory_id=f"m-task-{index}",
            scope=MemoryScope.SESSION,
            kind=(
                MemoryKind.TASK_RESULT
            ),
            content=f"result {index}",
            session_id="s1",
            source_task_id=(
                f"t{index}"
            ),
            key=(
                f"task-result:t{index}"
            ),
        )

        record = replace(
            record,
            updated_at=float(index),
        )

        store.save(record)

    service = (
        MemoryMaintenanceService(
            store,
            session_id="s1",
            artifact_store=(
                artifact_store
            ),
        )
    )

    result = service.consolidate(
        max_task_results=2,
        max_notes=20,
    )

    assert result["archived"] == 2

    records = store.list_session(
        "s1",
        include_inactive=True,
    )

    active = [
        record
        for record in records
        if record.status
        == MemoryStatus.ACTIVE
    ]

    archived = [
        record
        for record in records
        if record.status
        == MemoryStatus.ARCHIVED
    ]

    assert len(active) == 2
    assert len(archived) == 2

    assert {
        record.id
        for record in active
    } == {
        "m-task-2",
        "m-task-3",
    }


def test_excess_notes_are_archived(
    tmp_path,
):
    store, artifact_store = (
        make_stores(
            tmp_path
        )
    )

    for index in range(3):

        record = new_memory_record(
            memory_id=f"m-note-{index}",
            scope=MemoryScope.SESSION,
            kind=MemoryKind.NOTE,
            content=f"note {index}",
            session_id="s1",
            key=f"note-{index}",
        )

        record = replace(
            record,
            updated_at=float(index),
        )

        store.save(record)

    service = (
        MemoryMaintenanceService(
            store,
            session_id="s1",
            artifact_store=(
                artifact_store
            ),
        )
    )

    service.consolidate(
        max_task_results=20,
        max_notes=1,
    )

    active = store.list_session(
        "s1"
    )

    assert len(active) == 1

    assert (
        active[0].id
        == "m-note-2"
    )


def test_referenced_artifact_is_not_deleted(
    tmp_path,
):
    store, artifact_store = (
        make_stores(
            tmp_path
        )
    )

    artifact = (
        artifact_store.save_text(
            task_id="t1",
            name="result.txt",
            content="important",
        )
    )

    memory = new_memory_record(
        memory_id="m-reference",
        scope=MemoryScope.SESSION,
        kind=(
            MemoryKind.TASK_RESULT
        ),
        content="artifact reference",
        session_id="s1",
        source_task_id="t1",
        key="task-result:t1",
        artifact_ids=(
            artifact.id,
        ),
    )

    store.save(memory)

    service = (
        MemoryMaintenanceService(
            store,
            session_id="s1",
            artifact_store=(
                artifact_store
            ),
        )
    )

    deleted = service.gc_artifacts(
        grace_seconds=0,
    )

    assert deleted == 0

    assert (
        artifact_store.load(
            task_id="t1",
            artifact_id=artifact.id,
        )
        is not None
    )


def test_orphan_artifact_is_deleted(
    tmp_path,
):
    store, artifact_store = (
        make_stores(
            tmp_path
        )
    )

    artifact = (
        artifact_store.save_text(
            task_id="t1",
            name="orphan.txt",
            content="orphan",
        )
    )

    service = (
        MemoryMaintenanceService(
            store,
            session_id="s1",
            artifact_store=(
                artifact_store
            ),
        )
    )

    deleted = service.gc_artifacts(
        grace_seconds=0,
    )

    assert deleted == 1

    assert (
        artifact_store.load(
            task_id="t1",
            artifact_id=artifact.id,
        )
        is None
    )


def test_maintenance_run_returns_summary(
    tmp_path,
):
    store, artifact_store = (
        make_stores(
            tmp_path
        )
    )

    artifact_store.save_text(
        task_id="t1",
        name="orphan.txt",
        content="orphan",
    )

    service = (
        MemoryMaintenanceService(
            store,
            session_id="s1",
            artifact_store=(
                artifact_store
            ),
        )
    )

    result = service.run(
        max_task_results=20,
        max_notes=20,
        artifact_grace_seconds=0,
    )

    assert set(result) == {
        "superseded",
        "archived",
        "artifacts_deleted",
    }

    assert (
        result[
            "artifacts_deleted"
        ]
        == 1
    )