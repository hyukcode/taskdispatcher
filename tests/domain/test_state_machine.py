
import pytest

from tasker.domain.state_machine import (
    InvalidTransition,
    TaskStateMachine,
    TaskStatus,
)


def test_normal_execution():
    machine = TaskStateMachine()

    machine.transition(TaskStatus.READY)
    machine.transition(TaskStatus.RUNNING)
    machine.transition(TaskStatus.SUCCESS)

    assert machine.status == TaskStatus.SUCCESS


def test_invalid_transition():
    machine = TaskStateMachine()

    with pytest.raises(InvalidTransition):
        machine.transition(TaskStatus.SUCCESS)

    assert machine.status == TaskStatus.PENDING


def test_approval_flow():
    machine = TaskStateMachine(TaskStatus.RUNNING)

    machine.transition(TaskStatus.WAITING_APPROVAL)
    machine.transition(TaskStatus.RUNNING)
    machine.transition(TaskStatus.SUCCESS)

    assert machine.status == TaskStatus.SUCCESS


def test_pause_and_resume():
    machine = TaskStateMachine(TaskStatus.RUNNING)

    machine.transition(TaskStatus.PAUSED)
    machine.transition(TaskStatus.READY)
    machine.transition(TaskStatus.RUNNING)

    assert machine.status == TaskStatus.RUNNING
