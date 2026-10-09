import pytest

from tasker.artifact_store import (
    ArtifactStore,
)

from tasker.domain.memory import (
    MemoryKind,
    MemoryScope,
)

from tasker.memory_runtime import (
    MemoryManager,
)

from tasker.memory_store import (
    MemoryStore,
)

from tasker.models import (
    SubTask,
    TaskRun,
)


def make_manager(
    tmp_path,
    *,
    long_term_enabled=False,
    context_max_chars=6000,
    artifact_threshold_chars=500,
    artifact_preview_chars=100,
):
    memory_store = MemoryStore(
        session_base=(
            tmp_path / "sessions"
        ),
        long_term_base=(
            tmp_path / "long_term"
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

        session_limit=8,
        long_term_limit=4,

        task_result_max_chars=4000,

        artifact_threshold_chars=(
            artifact_threshold_chars
        ),

        artifact_preview_chars=(
            artifact_preview_chars
        ),

        context_max_chars=(
            context_max_chars
        ),

        long_term_enabled=(
            long_term_enabled
        ),
    )

    return (
        manager,
        memory_store,
        artifact_store,
    )


def make_task(
    task_id,
    title,
    description,
):
    return SubTask(
        id=task_id,
        title=title,
        description=description,
        executor="claude",
    )


def test_same_key_is_upserted(
    tmp_path,
):
    manager, store, _ = (
        make_manager(
            tmp_path
        )
    )

    first = manager.remember_decision(
        key="database",
        content=(
            "数据库使用 PostgreSQL"
        ),
    )

    second = manager.remember_decision(
        key="database",
        content=(
            "数据库改为 MySQL"
        ),
    )

    assert first.id == second.id

    records = store.list_session(
        "s1"
    )

    assert len(records) == 1

    assert (
        records[0].content
        == "数据库改为 MySQL"
    )


def test_fact_and_decision_have_different_identity(
    tmp_path,
):
    manager, store, _ = (
        make_manager(
            tmp_path
        )
    )

    fact = manager.remember_fact(
        key="database",
        content="数据库是 MySQL",
    )

    decision = (
        manager.remember_decision(
            key="database",
            content="决定继续使用 MySQL",
        )
    )

    assert fact.id != decision.id

    assert len(
        store.list_session(
            "s1"
        )
    ) == 2


def test_failed_task_is_not_remembered(
    tmp_path,
):
    manager, store, _ = (
        make_manager(
            tmp_path
        )
    )

    task = make_task(
        "t1",
        "失败任务",
        "模拟失败",
    )

    run = TaskRun(
        task=task,
        status="failed",
        output="failure output",
        exit_code=1,
    )

    result = (
        manager.record_task_run(
            run
        )
    )

    assert result is None

    assert (
        store.list_session("s1")
        == []
    )


def test_small_task_output_stays_in_memory(
    tmp_path,
):
    manager, _, artifact_store = (
        make_manager(
            tmp_path,
            artifact_threshold_chars=500,
        )
    )

    task = make_task(
        "t1",
        "分析任务",
        "分析代码",
    )

    run = TaskRun(
        task=task,
        status="success",
        output="small output",
        exit_code=0,
    )

    memory = (
        manager.record_task_run(
            run
        )
    )

    assert memory is not None

    assert (
        memory.artifact_ids
        == ()
    )

    assert (
        "small output"
        in memory.content
    )

    assert (
        artifact_store
        .list_for_task("t1")
        == []
    )


def test_large_task_output_becomes_artifact(
    tmp_path,
):
    manager, _, artifact_store = (
        make_manager(
            tmp_path,
            artifact_threshold_chars=500,
            artifact_preview_chars=100,
        )
    )

    task = make_task(
        "t1",
        "仓库分析",
        "分析整个仓库",
    )

    output = (
        "HEAD\n"
        + "A" * 2000
        + "\nTAIL-SECRET"
    )

    run = TaskRun(
        task=task,
        status="success",
        output=output,
        exit_code=0,
    )

    memory = (
        manager.record_task_run(
            run
        )
    )

    assert memory is not None

    assert len(
        memory.artifact_ids
    ) == 1

    assert (
        "TAIL-SECRET"
        not in memory.content
    )

    artifact = (
        artifact_store.load(
            task_id="t1",
            artifact_id=(
                memory.artifact_ids[0]
            ),
        )
    )

    assert artifact is not None

    assert (
        artifact_store.read_text(
            artifact
        )
        == output
    )


def test_large_then_small_rerun_clears_artifact_reference(
    tmp_path,
):
    manager, store, _ = (
        make_manager(
            tmp_path,
            artifact_threshold_chars=500,
        )
    )

    task = make_task(
        "t1",
        "任务",
        "测试重跑",
    )

    manager.record_task_run(
        TaskRun(
            task=task,
            status="success",
            output="A" * 2000,
            exit_code=0,
        )
    )

    updated = (
        manager.record_task_run(
            TaskRun(
                task=task,
                status="success",
                output="small result",
                exit_code=0,
            )
        )
    )

    assert updated is not None

    assert (
        updated.artifact_ids
        == ()
    )

    records = store.list_session(
        "s1"
    )

    assert (
        records[0].artifact_ids
        == ()
    )


def test_direct_dependency_uses_memory_preview_not_full_output(
    tmp_path,
):
    manager, _, _ = (
        make_manager(
            tmp_path,
            artifact_threshold_chars=500,
            artifact_preview_chars=100,
        )
    )

    t1 = make_task(
        "t1",
        "分析仓库",
        "分析代码",
    )

    t2 = make_task(
        "t2",
        "实现功能",
        "根据分析结果实现功能",
    )

    output = (
        "HEADER "
        + "A" * 2000
        + " TAIL-SECRET"
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

    context = (
        manager.build_working_memory(
            node=t2,
            dependency_runs=[run],
            session_state={},
        )
    )

    assert "[直接依赖]" in context

    assert "artifact_id=" in context

    assert (
        "TAIL-SECRET"
        not in context
    )


def test_irrelevant_memory_not_injected(
    tmp_path,
):
    manager, _, _ = (
        make_manager(
            tmp_path
        )
    )

    manager.remember_decision(
        key="frontend-style",
        content="前端使用 Tailwind",
    )

    task = make_task(
        "t1",
        "实现 Redis 缓存",
        "增加 Redis cache",
    )

    context = (
        manager.build_working_memory(
            node=task,
            dependency_runs=[],
            session_state={},
        )
    )

    assert (
        "Tailwind"
        not in context
    )


def test_relevant_session_memory_is_injected(
    tmp_path,
):
    manager, _, _ = (
        make_manager(
            tmp_path
        )
    )

    manager.remember_decision(
        key="database",
        content=(
            "订单数据库使用 MySQL"
        ),
    )

    task = make_task(
        "t1",
        "实现订单接口",
        "使用 MySQL 保存订单",
    )

    context = (
        manager.build_working_memory(
            node=task,
            dependency_runs=[],
            session_state={},
        )
    )

    assert (
        "订单数据库使用 MySQL"
        in context
    )


def test_feedback_is_included(
    tmp_path,
):
    manager, _, _ = (
        make_manager(
            tmp_path
        )
    )

    task = make_task(
        "t1",
        "修复接口",
        "修复 API",
    )

    context = (
        manager.build_working_memory(
            node=task,
            dependency_runs=[],
            session_state={
                "feedback":
                    "上一轮缺少异常处理"
            },
        )
    )

    assert "[上一轮反馈]" in context

    assert (
        "缺少异常处理"
        in context
    )


def test_long_term_disabled_not_injected(
    tmp_path,
):
    manager, _, _ = (
        make_manager(
            tmp_path,
            long_term_enabled=False,
        )
    )

    session_record = (
        manager.remember_decision(
            key="database",
            content="数据库使用 MySQL",
        )
    )

    manager.promote(
        session_record,
        key="database",
    )

    # Session Memory 本身仍然存在，
    # 为了只测试 Long-term，
    # 使用另一个 session manager。
    store = manager.store

    other = MemoryManager(
        store,
        session_id="s2",
        long_term_enabled=False,
    )

    task = make_task(
        "t1",
        "实现 MySQL 查询",
        "使用 MySQL",
    )

    context = (
        other.build_working_memory(
            node=task,
            dependency_runs=[],
            session_state={},
        )
    )

    assert (
        "数据库使用 MySQL"
        not in context
    )


def test_long_term_enabled_is_injected(
    tmp_path,
):
    manager, store, _ = (
        make_manager(
            tmp_path
        )
    )

    record = (
        manager.remember_decision(
            key="database",
            content="数据库使用 MySQL",
        )
    )

    manager.promote(
        record,
        key="database",
    )

    other = MemoryManager(
        store,
        session_id="s2",
        long_term_enabled=True,
    )

    task = make_task(
        "t1",
        "实现 MySQL 查询",
        "基于 MySQL 实现订单查询",
    )

    context = (
        other.build_working_memory(
            node=task,
            dependency_runs=[],
            session_state={},
        )
    )

    assert (
        "数据库使用 MySQL"
        in context
    )

    assert (
        "Long-term Memory"
        in context
    )


def test_memory_with_artifact_cannot_promote(
    tmp_path,
):
    manager, _, _ = (
        make_manager(
            tmp_path,
            artifact_threshold_chars=500,
        )
    )

    task = make_task(
        "t1",
        "大输出任务",
        "生成大报告",
    )

    memory = (
        manager.record_task_run(
            TaskRun(
                task=task,
                status="success",
                output="A" * 2000,
                exit_code=0,
            )
        )
    )

    assert memory is not None

    assert memory.artifact_ids

    with pytest.raises(
        ValueError
    ):
        manager.promote(
            memory,
            key="report",
        )


def test_context_budget_is_enforced(
    tmp_path,
):
    manager, _, _ = (
        make_manager(
            tmp_path,
            context_max_chars=500,
        )
    )

    manager.remember_session(
        kind=MemoryKind.NOTE,
        key="redis-note",
        content=(
            "Redis "
            + "A" * 2000
        ),
        tags=("redis",),
        priority=90,
    )

    task = make_task(
        "t1",
        "Redis",
        "Redis 缓存",
    )

    context = (
        manager.build_working_memory(
            node=task,
            dependency_runs=[],
            session_state={},
        )
    )

    assert len(context) <= 500

    assert (
        "Memory 截断"
        in context
    )


def test_higher_priority_wins_when_match_count_equal(
    tmp_path,
):
    manager, _, _ = (
        make_manager(
            tmp_path
        )
    )

    manager.remember_session(
        kind=MemoryKind.NOTE,
        key="redis-low",
        content="Redis low",
        priority=10,
    )

    manager.remember_session(
        kind=MemoryKind.NOTE,
        key="redis-high",
        content="Redis high",
        priority=90,
    )

    task = make_task(
        "t1",
        "Redis",
        "Redis",
    )

    context = (
        manager.build_working_memory(
            node=task,
            dependency_runs=[],
            session_state={},
        )
    )

    assert (
        context.index("Redis high")
        < context.index("Redis low")
    )