import pytest

from tasker.artifact_store import (
    ArtifactStore,
)

from tasker.domain.artifact import (
    ArtifactKind,
)

from tasker.artifact_store import (
    ArtifactCorrupted,
)

from tasker.domain.memory import (
    MemoryKind,
)

from tasker.memory_runtime import (
    MemoryManager,
)

from tasker.memory_store import (
    MemoryStore,
)

from tasker.models import (
    SubTask,
    TaskRun,
)


def test_save_and_read_text(
    tmp_path,
):

    workspace = (
        tmp_path
        / "workspace"
        / "s1"
    )

    store = ArtifactStore(
        workspace_root=workspace,
        session_id="s1",
    )

    record = store.save_text(
        task_id="t1",
        name="result.txt",
        content="hello artifact",
        kind=ArtifactKind.TASK_OUTPUT,
    )

    assert (
        record.session_id
        == "s1"
    )

    assert (
        record.source_task_id
        == "t1"
    )

    assert (
        record.size_bytes
        == len(
            "hello artifact"
            .encode("utf-8")
        )
    )

    content = store.read_text(
        record
    )

    assert content == (
        "hello artifact"
    )

def test_same_content_reuses_artifact(
    tmp_path,
):

    store = ArtifactStore(
        workspace_root=(
            tmp_path / "s1"
        ),
        session_id="s1",
    )

    first = store.save_text(
        task_id="t1",
        name="output.txt",
        content="same output",
    )

    second = store.save_text(
        task_id="t1",
        name="output.txt",
        content="same output",
    )

    assert first.id == second.id

    records = store.list_for_task(
        "t1"
    )

    assert len(records) == 1


def test_corrupted_payload_is_detected(
    tmp_path,
):

    workspace = (
        tmp_path
        / "s1"
    )

    store = ArtifactStore(
        workspace_root=workspace,
        session_id="s1",
    )

    record = store.save_text(
        task_id="t1",
        name="output.txt",
        content="original",
    )

    payload = (
        workspace
        / record.relative_path
    )

    payload.write_text(
        "tampered",
        encoding="utf-8",
    )

    with pytest.raises(
        ArtifactCorrupted
    ):
        store.read_text(
            record
        )



def test_large_task_output_becomes_artifact(
    tmp_path,
):

    session_root = (
        tmp_path / "sessions"
    )

    workspace = (
        tmp_path
        / "workspace"
        / "s1"
    )

    memory_store = MemoryStore(
        session_base=session_root,
        long_term_base=(
            tmp_path / "long"
        ),
    )

    artifact_store = ArtifactStore(
        workspace_root=workspace,
        session_id="s1",
    )

    manager = MemoryManager(
        memory_store,

        session_id="s1",

        artifact_store=(
            artifact_store
        ),

        artifact_threshold_chars=1000,

        artifact_preview_chars=200,
    )

    task = SubTask(
        id="t1",
        title="分析仓库",
        description="分析仓库代码",
    )

    output = "A" * 5000

    run = TaskRun(
        task=task,
        status="success",
        output=output,
        exit_code=0,
    )

    memory = manager.record_task_run(
        run
    )

    assert memory is not None

    assert (
        memory.kind
        == MemoryKind.TASK_RESULT
    )

    assert (
        len(memory.artifact_ids)
        == 1
    )

    assert len(memory.content) < 1500

    artifact = (
        artifact_store.load(
            task_id="t1",
            artifact_id=(
                memory.artifact_ids[0]
            ),
        )
    )

    assert artifact is not None

    assert (
        artifact_store.read_text(
            artifact
        )
        == output
    )