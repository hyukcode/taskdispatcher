from tasker.config import (
    Config,
)

from tasker.domain.tool import (
    ToolAccess,
    ToolSideEffect,
    ToolSpec,
)

from tasker.models import (
    SubTask,
    TaskRun,
)

from tasker.runner_base import (
    RunnerBase,
)

from tasker.tool_policy import (
    ToolPolicy,
    ToolPolicyAction,
)

from tasker.tool_registry import (
    ToolRegistry,
)


class DummyRunner(
    RunnerBase
):
    source = "claude"

    def _run_transport(
        self,
    ) -> None:
        pass


def make_runner(
    *,
    task: SubTask,
    policy: ToolPolicy,
):
    cfg = Config()

    run = TaskRun(
        task=task
    )

    return DummyRunner(
        cfg,
        run,

        "/tmp/tasker-test",

        lambda run, event: None,

        "test",

        tool_policy=policy,
    )


def test_runner_authorize_safe_tool():

    registry = ToolRegistry(
        [
            ToolSpec(
                name="Read",

                executors=frozenset(
                    {"claude"}
                ),
            )
        ]
    )

    policy = ToolPolicy(
        registry
    )

    task = SubTask(
        id="t1",

        title="read",

        description="read",

        executor="claude",

        workspace_access=(
            "read_only"
        ),
    )

    runner = make_runner(
        task=task,
        policy=policy,
    )

    decision = (
        runner.authorize_tool(
            "Read",
            {
                "path": "README.md",
            },
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.ALLOW
    )


def test_runner_denies_write_tool_for_read_only_task():

    registry = ToolRegistry(
        [
            ToolSpec(
                name="Edit",

                executors=frozenset(
                    {"claude"}
                ),

                access=(
                    ToolAccess.WRITE
                ),

                side_effect=(
                    ToolSideEffect
                    .LOCAL_WRITE
                ),
            )
        ]
    )

    policy = ToolPolicy(
        registry
    )

    task = SubTask(
        id="t1",

        title="read",

        description="read",

        executor="claude",

        workspace_access=(
            "read_only"
        ),
    )

    runner = make_runner(
        task=task,
        policy=policy,
    )

    decision = (
        runner.authorize_tool(
            "Edit",
            {
                "path": "a.py",
            },
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.DENY
    )

def test_runner_uses_injected_policy():

    registry = ToolRegistry(
        [
            ToolSpec(
                name="Read",
                executors=frozenset(
                    {"claude"}
                ),
            )
        ]
    )

    policy = ToolPolicy(
        registry
    )

    task = SubTask(
        id="t1",
        title="test",
        description="test",
        executor="claude",
    )

    runner = make_runner(
        task=task,
        policy=policy,
    )

    assert (
        runner.tool_policy
        is policy
    )