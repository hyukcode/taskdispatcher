from types import SimpleNamespace

import pytest

from tasker.checkpoint import (
    CheckpointStore,
)

from tasker.checkpoint_runtime import (
    AttemptOutcomeUnknown,
    CheckpointCoordinator,
    RecoveryBlocked,
)


def make_coordinator(
    tmp_path,
):
    store = CheckpointStore(
        tmp_path
    )

    coordinator = (
        CheckpointCoordinator(
            store,
            session_id="s1",
            plan_signature="plan1",
        )
    )

    return store, coordinator


def test_started_written_before_runner(
    tmp_path,
):
    store, coordinator = (
        make_coordinator(
            tmp_path
        )
    )

    runner_called = False

    def fake_runner():
        nonlocal runner_called

        runner_called = True

        checkpoint = store.load(
            "s1",
            "t1",
            "a1",
        )

        assert checkpoint is not None

        assert (
            checkpoint.status
            == "started"
        )

        return SimpleNamespace(
            status="success",
            exit_code=0,
            error="",
        )

    result = coordinator.run_attempt(
        task_id="t1",
        execution_id="e1",
        attempt_id="a1",
        executor="claude",
        invoke=fake_runner,
    )

    assert runner_called

    assert result.status == "success"

    checkpoint = store.load(
        "s1",
        "t1",
        "a1",
    )

    assert checkpoint is not None

    assert (
        checkpoint.status
        == "success"
    )


def test_exception_leaves_started(
    tmp_path,
):
    store, coordinator = (
        make_coordinator(
            tmp_path
        )
    )

    def fake_runner():
        raise ConnectionError(
            "connection lost"
        )

    with pytest.raises(
        AttemptOutcomeUnknown
    ):
        coordinator.run_attempt(
            task_id="t1",
            execution_id="e1",
            attempt_id="a1",
            executor="claude",
            invoke=fake_runner,
        )

    checkpoint = store.load(
        "s1",
        "t1",
        "a1",
    )

    assert checkpoint is not None

    assert (
        checkpoint.status
        == "started"
    )


def test_preflight_blocks_started(
    tmp_path,
):
    store, coordinator = (
        make_coordinator(
            tmp_path
        )
    )

    store.start(
        session_id="s1",
        task_id="t1",
        execution_id="e1",
        attempt_id="a1",
        executor="claude",
        plan_signature="plan1",
    )

    with pytest.raises(
        RecoveryBlocked
    ) as captured:

        coordinator.preflight(
            ["t1"],
            {},
            is_resuming=True,
        )

    issue = (
        captured.value
        .issues[0]
    )

    assert (
        issue.reason
        == "RECOVERY_REQUIRED"
    )


def test_pending_task_without_checkpoint_is_safe(
    tmp_path,
):
    _, coordinator = (
        make_coordinator(
            tmp_path
        )
    )

    # 没 checkpoint
    # 没 TaskRun
    #
    # 表示很可能根本没开始执行。
    coordinator.preflight(
        ["t1"],
        {},
        is_resuming=True,
    )


def test_success_checkpoint_and_snapshot_match(
    tmp_path,
):
    store, coordinator = (
        make_coordinator(
            tmp_path
        )
    )

    store.start(
        session_id="s1",
        task_id="t1",
        execution_id="e1",
        attempt_id="a1",
        executor="claude",
        plan_signature="plan1",
    )

    store.finish(
        "s1",
        "t1",
        "a1",
        success=True,
        exit_code=0,
    )

    coordinator.preflight(
        ["t1"],
        {
            "t1": {
                "status": "success",
                "attempt_id": "a1",
            }
        },
        is_resuming=True,
    )


def test_success_without_snapshot_is_blocked(
    tmp_path,
):
    store, coordinator = (
        make_coordinator(
            tmp_path
        )
    )

    store.start(
        session_id="s1",
        task_id="t1",
        execution_id="e1",
        attempt_id="a1",
        executor="claude",
        plan_signature="plan1",
    )

    store.finish(
        "s1",
        "t1",
        "a1",
        success=True,
        exit_code=0,
    )

    with pytest.raises(
        RecoveryBlocked
    ) as captured:

        coordinator.preflight(
            ["t1"],
            {},
            is_resuming=True,
        )

    assert (
        captured.value
        .issues[0]
        .reason
        == "SUCCESS_WITHOUT_SNAPSHOT"
    )


def test_failed_attempt_requires_review(
    tmp_path,
):
    store, coordinator = (
        make_coordinator(
            tmp_path
        )
    )

    store.start(
        session_id="s1",
        task_id="t1",
        execution_id="e1",
        attempt_id="a1",
        executor="claude",
        plan_signature="plan1",
    )

    store.finish(
        "s1",
        "t1",
        "a1",
        success=False,
        exit_code=1,
        error="timeout",
    )

    with pytest.raises(
        RecoveryBlocked
    ) as captured:

        coordinator.preflight(
            ["t1"],
            {},
            is_resuming=True,
        )

    assert (
        captured.value
        .issues[0]
        .reason
        == "REVIEW_BEFORE_RETRY"
    )