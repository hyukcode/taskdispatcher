import pytest
from tasker.checkpoint import CheckpointStore, CheckpointConflict

PARAMS = dict(session_id="s1", task_id="t1", execution_id="e1", attempt_id="a1", executor="claude", plan_signature="sig1")


def test_save_and_reload(tmp_path):
    store = CheckpointStore(tmp_path)
    store.start(**PARAMS)
    reloaded = CheckpointStore(tmp_path)
    cp = reloaded.load("s1", "t1", "a1")
    assert cp.status == "started"
    assert reloaded.recovery_decision("s1", "t1", "a1", "sig1") == "RECOVERY_REQUIRED"


def test_success_and_idempotent_finish(tmp_path):
    store = CheckpointStore(tmp_path)
    store.start(**PARAMS)
    first = store.finish("s1", "t1", "a1", success=True, exit_code=0)
    second = store.finish("s1", "t1", "a1", success=True, exit_code=0)
    assert first == second
    assert store.recovery_decision("s1", "t1", "a1", "sig1") == "REUSE_RESULT"


def test_failed_requires_review_before_retry(tmp_path):
    store = CheckpointStore(tmp_path)
    store.start(**PARAMS)
    store.finish("s1", "t1", "a1", success=False, exit_code=1, error="timeout")
    assert store.recovery_decision("s1", "t1", "a1", "sig1") == "REVIEW_BEFORE_RETRY"


def test_duplicate_start_and_conflicting_finish(tmp_path):
    store = CheckpointStore(tmp_path)
    store.start(**PARAMS)
    with pytest.raises(CheckpointConflict):
        store.start(**PARAMS)
    store.finish("s1", "t1", "a1", success=True)
    with pytest.raises(CheckpointConflict):
        store.finish("s1", "t1", "a1", success=False)


def test_plan_change_and_invalid_id(tmp_path):
    store = CheckpointStore(tmp_path)
    store.start(**PARAMS)
    assert store.recovery_decision("s1", "t1", "a1", "new-signature") == "PLAN_CHANGED"
    with pytest.raises(ValueError):
        store.load("../evil", "t1", "a1")


def test_missing_and_corrupt(tmp_path):
    store = CheckpointStore(tmp_path)
    assert store.recovery_decision("s1", "t1", "missing", "sig1") == "NOT_FOUND"
    file = tmp_path / "s1" / "checkpoints" / "t1" / "a1.json"
    file.parent.mkdir(parents=True)
    file.write_text("{garbled", encoding="utf-8")
    with pytest.raises(ValueError):
        store.load("s1", "t1", "a1")


def test_find_attempt_when_session_snapshot_has_no_attempt_id(tmp_path):
    store = CheckpointStore(tmp_path)
    store.start(**PARAMS)
    assert [cp.attempt_id for cp in store.list_for_task("s1", "t1")] == ["a1"]
    assert store.inspect_execution("s1", "t1", "e1", "sig1") == "RECOVERY_REQUIRED"
    store.finish("s1", "t1", "a1", success=True, exit_code=0)
    assert store.inspect_execution("s1", "t1", "e1", "sig1") == "REUSE_RESULT"
