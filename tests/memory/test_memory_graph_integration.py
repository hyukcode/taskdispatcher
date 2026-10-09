from tasker.approvals import (
    ApprovalBroker,
)

from tasker.artifact_store import (
    ArtifactStore,
)

from tasker.config import Config

from tasker.graph_executor import (
    GraphExecutor,
)

from tasker.memory_runtime import (
    MemoryManager,
)

from tasker.memory_store import (
    MemoryStore,
)

from tasker.models import (
    CompiledGraph,
    GraphEdge,
    SubTask,
    TaskRun,
)


def test_graph_executor_injects_working_memory(
    tmp_path,
):
    cfg = Config()

    broker = ApprovalBroker(
        cfg.approval
    )

    t1 = SubTask(
        id="t1",
        title="确定数据库",
        description="确定数据库方案",
        executor="claude",
    )

    t2 = SubTask(
        id="t2",
        title="实现 MySQL Repository",
        description=(
            "根据数据库方案实现 "
            "MySQL Repository"
        ),
        executor="claude",
    )

    graph = CompiledGraph(
        nodes=[
            t1,
            t2,
        ],
        edges=[
            GraphEdge(
                src="t1",
                dst="t2",
            )
        ],
        entry="t1",
    )

    memory_store = MemoryStore(
        session_base=(
            tmp_path / "sessions"
        ),
        long_term_base=(
            tmp_path / "long"
        ),
    )

    artifact_store = ArtifactStore(
        workspace_root=(
            tmp_path
            / "workspace"
            / "s1"
        ),
        session_id="s1",
    )

    manager = MemoryManager(
        memory_store,

        session_id="s1",

        artifact_store=(
            artifact_store
        ),

        artifact_threshold_chars=500,

        artifact_preview_chars=100,
    )

    run = TaskRun(
        task=t1,
        status="success",
        output=(
            "数据库最终选择 MySQL"
        ),
        exit_code=0,
    )

    manager.record_task_run(
        run
    )

    executor = GraphExecutor(
        cfg,
        graph,
        broker,

        workdir=(
            tmp_path
            / "workspace"
            / "s1"
        ),

        session_id="s1",

        memory_manager=manager,
    )

    executor.runs["t1"] = run

    prompt = executor._prompt_for(
        t2
    )

    assert (
        "当前任务 Working Memory"
        in prompt
    )

    assert (
        "数据库最终选择 MySQL"
        in prompt
    )

    assert "[直接依赖]" in prompt


def test_large_dependency_does_not_leak_full_output_into_prompt(
    tmp_path,
):
    cfg = Config()

    broker = ApprovalBroker(
        cfg.approval
    )

    t1 = SubTask(
        id="t1",
        title="生成大型分析",
        description="分析仓库",
        executor="claude",
    )

    t2 = SubTask(
        id="t2",
        title="根据分析实现功能",
        description="使用分析结果实现功能",
        executor="claude",
    )

    graph = CompiledGraph(
        nodes=[
            t1,
            t2,
        ],
        edges=[
            GraphEdge(
                src="t1",
                dst="t2",
            )
        ],
        entry="t1",
    )

    store = MemoryStore(
        session_base=(
            tmp_path / "sessions"
        ),
        long_term_base=(
            tmp_path / "long"
        ),
    )

    artifact_store = ArtifactStore(
        workspace_root=(
            tmp_path
            / "workspace"
            / "s1"
        ),
        session_id="s1",
    )

    manager = MemoryManager(
        store,

        session_id="s1",

        artifact_store=(
            artifact_store
        ),

        artifact_threshold_chars=500,

        artifact_preview_chars=100,
    )

    output = (
        "REPORT-BEGIN "
        + "A" * 5000
        + " REPORT-END-SECRET"
    )

    run = TaskRun(
        task=t1,
        status="success",
        output=output,
        exit_code=0,
    )

    manager.record_task_run(
        run
    )

    executor = GraphExecutor(
        cfg,
        graph,
        broker,

        workdir=(
            tmp_path
            / "workspace"
            / "s1"
        ),

        session_id="s1",

        memory_manager=manager,
    )

    executor.runs["t1"] = run

    prompt = executor._prompt_for(
        t2
    )

    assert "artifact_id=" in prompt

    assert (
        "REPORT-END-SECRET"
        not in prompt
    )

    assert len(prompt) < len(output)


def test_unrelated_memory_does_not_pollute_prompt(
    tmp_path,
):
    cfg = Config()

    broker = ApprovalBroker(
        cfg.approval
    )

    task = SubTask(
        id="t1",
        title="Redis 缓存",
        description="实现 Redis 缓存",
        executor="claude",
    )

    graph = CompiledGraph(
        nodes=[task],
        entry="t1",
    )

    store = MemoryStore(
        session_base=(
            tmp_path / "sessions"
        ),
        long_term_base=(
            tmp_path / "long"
        ),
    )

    manager = MemoryManager(
        store,
        session_id="s1",
    )

    manager.remember_decision(
        key="frontend-style",
        content="前端使用 Tailwind",
    )

    executor = GraphExecutor(
        cfg,
        graph,
        broker,

        workdir=(
            tmp_path
            / "workspace"
            / "s1"
        ),

        session_id="s1",

        memory_manager=manager,
    )

    prompt = executor._prompt_for(
        task
    )

    assert (
        "Tailwind"
        not in prompt
    )