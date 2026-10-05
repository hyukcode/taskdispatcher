
from tasker.models import SubTask

from tasker.domain.execution import TaskExecution
from tasker.domain.state_machine import TaskStatus


def test_retry_with_another_executor():
    task = SubTask(
        id="t1",
        title="实现订单接口",
        description="使用 FastAPI 实现订单创建接口",
        executor="claude",
    )

    execution = TaskExecution(
        session_id="session-001",
        task=task,
    )

    # 第一次执行
    execution.mark_ready()
    first = execution.start_attempt()

    assert first.executor == "claude"
    assert execution.status == TaskStatus.RUNNING

    execution.finish_attempt(
        success=False,
        error="执行超时",
        exit_code=1,
    )

    assert execution.status == TaskStatus.FAILED

    # 重试策略允许切换到 Codex
    execution.mark_ready()
    second = execution.start_attempt("codex")

    assert second.parent_attempt_id == first.id

    execution.finish_attempt(
        success=True,
        output="订单接口已经完成",
        exit_code=0,
    )

    assert execution.status == TaskStatus.SUCCESS
    assert len(execution.attempts) == 2

    assert execution.attempts[0].status == "failed"
    assert execution.attempts[1].status == "success"
