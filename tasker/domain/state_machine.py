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
    CANCALLED = "cancelled"
    SKIPPED = "skipped"

# 允许流转的状态
TRANSITIONS = {
    TaskStatus.PENDING: {
        TaskStatus.READY,
        TaskStatus.CANCALLED,
        TaskStatus.SKIPPED,
    },
    TaskStatus.READY: {
        TaskStatus.RUNNING,
        TaskStatus.CANCALLED,
        TaskStatus.SKIPPED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.PAUSED,
        TaskStatus.SUCCESS,
        TaskStatus.FAILED,
        TaskStatus.CANCALLED,
    },
    TaskStatus.WAITING_APPROVAL: {
        TaskStatus.RUNNING,
        TaskStatus.PAUSED,
        TaskStatus.FAILED,
        TaskStatus.CANCALLED,
    },
    TaskStatus.PAUSED: {
        TaskStatus.READY,
        TaskStatus.CANCALLED,
    },
    TaskStatus.FAILED: {
        TaskStatus.READY,
        TaskStatus.CANCALLED,
    },
    TaskStatus.SUCCESS: set(),
    TaskStatus.CANCALLED: set(),
    TaskStatus.SKIPPED: set(),
}

class InvalidTransition(Exception):
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
    
