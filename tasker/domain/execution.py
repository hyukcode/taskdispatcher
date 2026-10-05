from __future__ import annotations

import time
import uuid

from dataclasses import dataclass, field, replace, asdict
from typing import Optional

from tasker.models import SubTask

from .state_machine import (
    TaskStateMachine,
    TaskStatus,
    InvalidTransition,
)

# Task -> TaskExecution -> Attempt 1 Attempt 2 Attempt 3

@dataclass(frozen=True)
class Attempt:
    id: str
    executor: str
    number: int
    parent_attempt_id: str = ""
    status: str = "running"
    started_at: float = 0.0
    ended_at: Optional[float] = None
    output: str = ""
    error: str = ""
    exit_code: Optional[int] = None
    failure_class: str = ""
    cost_usd: float = 0.0

# 一次 Task 在当前 Session 中的一次整体执行
@dataclass
class TaskExecution:
    session_id: str
    task: SubTask
    execution_id: str = field(
        default_factory=lambda: uuid.uuid4().hex
    )
    machine: TaskStateMachine = field(
        default_factory=TaskStateMachine
    )
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def status(self) -> TaskStatus:
        return self.machine.status
    
    def mark_ready(self) -> None:
        self.machine.transition(TaskStatus.READY)
    
    def start_attempt(
        self,
        executor: str | None = None,
        attempt_id: str | None = None,
    ) -> Attempt:
        number = len(self.attempts) + 1
        parent_id = self.attempts[-1].id if self.attempts else ""
        candidate = attempt_id or (
            f"{self.task.id}-a{number}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        if any(a.id == candidate for a in self.attempts):
            raise ValueError("duplicate attempt_id")
        self.machine.transition(TaskStatus.RUNNING)
        attempt = Attempt(
            id=candidate,
            executor=executor or self.task.executor,
            number=number,
            parent_attempt_id=parent_id,
            started_at=time.time(),
        )
        self.attempts.append(attempt)
        return attempt
    
    def finish_attempt(
        self,
        *,
        success: bool,
        output: str = "",
        error: str = "",
        exit_code: int | None = None,
        failure_class: str = "",
        cost_usd: float = 0.0,
    ) -> Attempt:

        if self.status != TaskStatus.RUNNING:
            raise InvalidTransition(
                "Only running tasks can finish"
            )

        if not self.attempts:
            raise ValueError("No active attempt")

        current = self.attempts[-1]

        if current.status != "running":
            raise ValueError("Attempt already finished")
        
        finished = replace(
            current,
            status="success" if success else "failed",
            ended_at=time.time(),
            output=output,
            error=error,
            exit_code=exit_code,
            failure_class=failure_class,
            cost_usd=cost_usd,
        )

        self.machine.transition(
            TaskStatus.SUCCESS if success
            else TaskStatus.FAILED
        )

        self.attempts[-1] = finished
        return finished

    def pause_attempt(
        self,
        *,
        error: str = "",
    ) -> Attempt:
        if self.status != Task.RUNNING:
            raise InvalidTransition(
                "only running execution can be paused"
            )
        if not self.attempts:
            raise ValueError(
                "no active attempt"
            )
        current = self.attempts[-1]
        paused = replace(
            current,
            status="unknown",
            ended_at=time.time(),
            error=error,
        )
        self.attempts[-1] = paused
        self.machine.transition(TaskStatus.PAUSED)
        return paused

    def cancel(self) -> None:
        self.machine.transition(TaskStatus.CANCELLED)

    # 持久化和中断恢复的数据基础 session_id 管理上下文 task_id 管理业务目标 execution_id 管理具体执行过程
    def snapshot(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "session_id": self.session_id,
            "task_id": self.task.id,
            "status": self.status.value,
            "attempts": [
                asdict(a) for a in self.attempts
            ]
        }
