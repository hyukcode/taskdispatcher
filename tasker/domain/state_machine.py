from enum import Enum 


# 有限状态集合 多重继承和枚举
class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

# 允许流转的状态
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {
        TaskStatus.READY,
        TaskStatus.CANCELLED,
        TaskStatus.SKIPPED,
    },
    TaskStatus.READY: {
        TaskStatus.RUNNING,
        TaskStatus.CANCELLED,
        TaskStatus.SKIPPED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.PAUSED,
        TaskStatus.SUCCESS,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.WAITING_APPROVAL: {
        TaskStatus.RUNNING,
        TaskStatus.PAUSED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.PAUSED: {
        TaskStatus.READY,
        TaskStatus.CANCELLED,
    },
    TaskStatus.FAILED: {
        TaskStatus.READY,
        TaskStatus.CANCELLED,
    },
    TaskStatus.SUCCESS: set(),
    TaskStatus.CANCELLED: set(),
    TaskStatus.SKIPPED: set(),
}

class InvalidTransition(RuntimeError):
    pass


# 状态变化的校验
class TaskStateMachine:
    def __init__(
        self,
        status: TaskStatus = TaskStatus.PENDING,
    ):
        self.status = status
    
    def transition(self, target: TaskStatus) -> None:
        allowed = TRANSITIONS[self.status]
        if target not in allowed:
            raise InvalidTransition(
                f"{self.status.value} -> "
                f"{target.value} is not allowed"
            )
        self.status = target
    
