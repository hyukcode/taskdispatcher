import pytest

from tasker.artifact_store import (
    ArtifactCorrupted,
    ArtifactStore,
)

from tasker.domain.artifact import (
    ArtifactKind,
)


def make_store(tmp_path):
    return ArtifactStore(
        workspace_root=(
            tmp_path
            / "workspace"
            / "s1"
        ),
        session_id="s1",
    )


def test_save_and_read_artifact(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    record = store.save_text(
        task_id="t1",
        name="result.txt",
        content="hello artifact",
        kind=(
            ArtifactKind.TASK_OUTPUT
        ),
    )

    assert record.id.startswith(
        "a-"
    )

    assert record.session_id == "s1"

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

    assert (
        store.read_text(record)
        == "hello artifact"
    )


def test_same_content_is_idempotent(
    tmp_path,
):
    store = make_store(
        tmp_path
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

    assert len(
        store.list_for_task(
            "t1"
        )
    ) == 1


def test_different_content_creates_different_artifact(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    first = store.save_text(
        task_id="t1",
        name="output.txt",
        content="version one",
    )

    second = store.save_text(
        task_id="t1",
        name="output.txt",
        content="version two",
    )

    assert (
        first.id
        != second.id
    )

    assert len(
        store.list_for_task(
            "t1"
        )
    ) == 2


def test_read_can_be_truncated(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    record = store.save_text(
        task_id="t1",
        name="large.txt",
        content="A" * 1000,
    )

    content = store.read_text(
        record,
        max_chars=100,
    )

    assert content.startswith(
        "A" * 100
    )

    assert (
        "Artifact 截断"
        in content
    )


def test_corrupted_payload_detected(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    record = store.save_text(
        task_id="t1",
        name="result.txt",
        content="original",
    )

    payload_path = (
        store.workspace_root
        / record.relative_path
    )

    payload_path.write_text(
        "tampered",
        encoding="utf-8",
    )

    with pytest.raises(
        ArtifactCorrupted
    ):
        store.read_text(
            record
        )


def test_missing_payload_detected(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    record = store.save_text(
        task_id="t1",
        name="result.txt",
        content="hello",
    )

    payload_path = (
        store.workspace_root
        / record.relative_path
    )

    payload_path.unlink()

    with pytest.raises(
        ArtifactCorrupted
    ):
        store.read_text(
            record
        )


def test_list_all(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    store.save_text(
        task_id="t1",
        name="one.txt",
        content="one",
    )

    store.save_text(
        task_id="t2",
        name="two.txt",
        content="two",
    )

    records = store.list_all()

    assert len(records) == 2

    assert {
        record.source_task_id
        for record in records
    } == {
        "t1",
        "t2",
    }


def test_delete_artifact(
    tmp_path,
):
    store = make_store(
        tmp_path
    )

    record = store.save_text(
        task_id="t1",
        name="delete.txt",
        content="delete",
    )

    assert store.delete(
        record
    )

    assert (
        store.load(
            task_id="t1",
            artifact_id=record.id,
        )
        is None
    )

    assert not store.delete(
        record
    )

