from tasker import goal_loop as goal_loop_module

from tasker.approvals import (
    ApprovalBroker,
)

from tasker.config import (
    Config,
    SessionConfig,
)

from tasker.goal_loop import (
    GoalLoop,
)

from tasker.models import (
    CompiledGraph,
    Session,
    SubTask,
    TaskRun,
)

from tasker.session import (
    SessionStore,
)


def test_memory_failure_does_not_destroy_task_success(
    tmp_path,
    monkeypatch,
):
    cfg = Config()

    cfg.session = SessionConfig(
        dir=str(
            tmp_path / "sessions"
        ),
        workspace_dir=str(
            tmp_path / "workspace"
        ),
    )

    cfg.memory.long_term_dir = str(
        tmp_path / "long_term"
    )

    task = SubTask(
        id="t1",
        title="测试任务",
        description="完成测试任务",
        executor="claude",
    )

    run = TaskRun(
        task=task,
        status="success",
        output="done",
        exit_code=0,
    )

    graph = CompiledGraph(
        nodes=[task],
        entry="t1",
    )

    session = Session(
        session_id="s1",
        goal="test",
        plan_signature="plan-one",
    )

    store = SessionStore(
        cfg.session
    )

    broker = ApprovalBroker(
        cfg.approval
    )

    class BrokenMemoryManager:

        def __init__(
            self,
            *args,
            **kwargs,
        ):
            pass

        def record_task_run(
            self,
            run,
        ):
            raise OSError(
                "memory disk failure"
            )

    class FakeExecutor:

        def __init__(
            self,
            *args,
            on_task_complete=None,
            **kwargs,
        ):
            self.on_task_complete = (
                on_task_complete
            )

        def execute(self):
            assert (
                self.on_task_complete
                is not None
            )

            self.on_task_complete(
                run
            )

            return [run]

    monkeypatch.setattr(
        goal_loop_module,
        "MemoryManager",
        BrokenMemoryManager,
    )

    monkeypatch.setattr(
        goal_loop_module,
        "GraphExecutor",
        FakeExecutor,
    )

    loop = GoalLoop(
        cfg,
        broker,
        store,
    )

    result = loop._execute_graph(
        graph,
        session,
        persist_runs=True,
    )

    assert result == [run]

    assert (
        session.task_runs["t1"]
        ["status"]
        == "success"
    )

    saved = store.load(
        "s1"
    )

    assert saved is not None

    assert (
        saved.task_runs["t1"]
        ["status"]
        == "success"
    )