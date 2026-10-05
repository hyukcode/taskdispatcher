
import time

from tasker.config import Config
from tasker.approvals import ApprovalBroker
from tasker.graph_executor import GraphExecutor
from tasker.models import (
    SubTask,
    CompiledGraph,
    TaskRun,
)


def test_failover_preserves_attempt_history(
    monkeypatch,
    tmp_path,
):
    cfg = Config()
    cfg.retry.max_retries = 0
    cfg.dispatch.failover_enabled = True

    task = SubTask(
        id="t1",
        title="实现接口",
        description="实现订单 API",
        executor="claude",
    )

    graph = CompiledGraph(
        nodes=[task],
        edges=[],
        entry="t1",
    )

    executor = GraphExecutor(
        cfg,
        graph,
        ApprovalBroker(cfg.approval),
        workdir=tmp_path,
        session_id="session-001",
        emit=lambda run, event: None,
    )

    calls = []

    def fake_run(node, context=""):
        calls.append(node.executor)

        success = node.executor == "codex"
        now = time.time()

        return TaskRun(
            task=node,
            status="success" if success else "failed",
            output="done" if success else "",
            error="" if success else "connection lost",
            exit_code=0 if success else 1,
            started_at=now,
            ended_at=now,
        )

    monkeypatch.setattr(
        executor,
        "_run_code_node",
        fake_run,
    )

    result = executor._run_code_node_with_failover(task)

    history = executor.execution_history["t1"]
    execution = history[0]

    assert calls == ["claude", "codex"]
    assert result.status == "success"
    assert len(execution.attempts) == 2

    first, second = execution.attempts

    assert first.status == "failed"
    assert second.status == "success"
    assert second.parent_attempt_id == first.id
