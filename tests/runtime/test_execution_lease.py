import json
import time

import pytest

from tasker.execution_lease import (
    ExecutionLease,
    LeaseHeldError,
)

from tasker.checkpoint import CheckpointStore
from tasker.checkpoint_runtime import CheckpointCoordinator


def test_second_owner_is_blocked(
    tmp_path,
):
    first = ExecutionLease(
        tmp_path,
        session_id="s1",
        ttl_seconds=10,
        heartbeat_seconds=1,
    )

    second = ExecutionLease(
        tmp_path,
        session_id="s1",
        ttl_seconds=10,
        heartbeat_seconds=1,
    )

    first.acquire()

    try:
        with pytest.raises(
            LeaseHeldError
        ):
            second.acquire()

    finally:
        first.release()


def test_release_allows_next_owner(
    tmp_path,
):
    first = ExecutionLease(
        tmp_path,
        session_id="s1",
        ttl_seconds=10,
        heartbeat_seconds=1,
    )

    first.acquire()
    first.release()

    second = ExecutionLease(
        tmp_path,
        session_id="s1",
        ttl_seconds=10,
        heartbeat_seconds=1,
    )

    try:
        second.acquire()

        assert second.acquired

    finally:
        second.release()


def test_stale_lease_can_be_taken_over(
    tmp_path,
):

    first = ExecutionLease(
        tmp_path,
        session_id="s1",
        ttl_seconds=10,
        heartbeat_seconds=1,
    )

    first.acquire()

    first._stop.set()

    if first._thread is not None:
        first._thread.join(
            timeout=2
        )

    path = (
        tmp_path
        / ".execution-lease"
        / "lease.json"
    )

    data = json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )

    data["heartbeat_at"] = (
        time.time() - 60
    )

    path.write_text(
        json.dumps(data),
        encoding="utf-8",
    )

    second = ExecutionLease(
        tmp_path,
        session_id="s1",
        ttl_seconds=10,
        heartbeat_seconds=1,
    )

    try:
        second.acquire()

        assert second.acquired

    finally:
        second.release()

        # first 已经失去所有权，
        # release 不能删 second 的 lease。
        first.release()

def test_abandon_is_idempotent(
    tmp_path,
):
    store = CheckpointStore(
        tmp_path
    )

    store.start(
        session_id="s1",
        task_id="t1",
        execution_id="e1",
        attempt_id="a1",
        executor="claude",
        plan_signature="p1",
    )

    first = store.abandon_for_retry(
        "s1",
        "t1",
        "a1",
        reason="checked",
    )

    second = store.abandon_for_retry(
        "s1",
        "t1",
        "a1",
        reason="checked again",
    )

    assert first.status == "abandoned"
    assert second.status == "abandoned"

    assert (
        first.resolved_at
        == second.resolved_at
    )

def test_success_can_be_superseded(
    tmp_path,
):
    store = CheckpointStore(
        tmp_path
    )

    store.start(
        session_id="s1",
        task_id="t1",
        execution_id="e1",
        attempt_id="a1",
        executor="claude",
        plan_signature="p1",
    )

    store.finish(
        "s1",
        "t1",
        "a1",
        success=True,
        exit_code=0,
    )

    checkpoint = store.supersede(
        "s1",
        "t1",
        "a1",
        reason="manual restart",
    )

    assert (
        checkpoint.status
        == "superseded"
    )

def test_preflight_ignores_abandoned(
    tmp_path,
):
    store = CheckpointStore(
        tmp_path
    )

    coordinator = (
        CheckpointCoordinator(
            store,
            session_id="s1",
            plan_signature="p1",
        )
    )

    store.start(
        session_id="s1",
        task_id="t1",
        execution_id="e1",
        attempt_id="a1",
        executor="claude",
        plan_signature="p1",
    )

    store.abandon_for_retry(
        "s1",
        "t1",
        "a1",
        reason="safe",
    )

    coordinator.preflight(
        ["t1"],
        {},
        is_resuming=True,
    )

def test_preflight_ignores_superseded(
    tmp_path,
):
    store = CheckpointStore(
        tmp_path
    )

    coordinator = (
        CheckpointCoordinator(
            store,
            session_id="s1",
            plan_signature="p1",
        )
    )

    store.start(
        session_id="s1",
        task_id="t1",
        execution_id="e1",
        attempt_id="a1",
        executor="claude",
        plan_signature="p1",
    )

    store.finish(
        "s1",
        "t1",
        "a1",
        success=True,
        exit_code=0,
    )

    store.supersede(
        "s1",
        "t1",
        "a1",
        reason="restart",
    )

    coordinator.preflight(
        ["t1"],
        {},
        is_resuming=True,
    )

