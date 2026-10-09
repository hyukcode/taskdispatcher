import pytest

from tasker.domain.tool import (
    ApprovalRequirement,
    ToolAccess,
    ToolIdempotency,
    ToolSideEffect,
    ToolSpec,
)

from tasker.policy_hooks import (
    HookChain,
    HookRule,
)

from tasker.tool_policy import (
    ToolPolicy,
    ToolPolicyAction,
    ToolPolicyContext,
)

from tasker.tool_registry import (
    ToolRegistry,
    default_tool_registry,
)


def context(
    *,
    executor="claude",
    tool_name="Read",
    workspace_access="write",
    workdir_scope="session",
    input_data=None,
):
    return ToolPolicyContext(
        executor=executor,

        task_id="t1",

        attempt_id="a1",

        tool_name=tool_name,

        input_data=(
            input_data
            or {}
        ),

        workspace_access=(
            workspace_access
        ),

        workdir_scope=(
            workdir_scope
        ),

        workdir="/tmp/work",
    )


def test_safe_read_tool_is_allowed():
    policy = ToolPolicy(
        default_tool_registry()
    )

    decision = policy.authorize(
        context(
            tool_name="Read"
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.ALLOW
    )

    assert (
        decision.canonical_name
        == "Read"
    )


def test_alias_resolves_to_canonical_tool():
    policy = ToolPolicy(
        default_tool_registry()
    )

    decision = policy.authorize(
        context(
            executor="claude",
            tool_name="read_file",
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.ALLOW
    )

    assert (
        decision.canonical_name
        == "Read"
    )


def test_write_tool_denied_for_read_only_task():
    policy = ToolPolicy(
        default_tool_registry()
    )

    decision = policy.authorize(
        context(
            tool_name="Edit",

            workspace_access=(
                "read_only"
            ),
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.DENY
    )

    assert (
        "read-only"
        in decision.reason
    )


def test_write_tool_requires_approval():
    policy = ToolPolicy(
        default_tool_registry()
    )

    decision = policy.authorize(
        context(
            tool_name="Edit"
        )
    )

    assert (
        decision.action
        ==
        ToolPolicyAction
        .REQUIRE_APPROVAL
    )


def test_unknown_tool_requires_approval_in_compatibility_mode():
    policy = ToolPolicy(
        default_tool_registry(),
        strict_registry=False,
    )

    decision = policy.authorize(
        context(
            tool_name=(
                "dynamic_plugin_tool"
            )
        )
    )

    assert (
        decision.action
        ==
        ToolPolicyAction
        .REQUIRE_APPROVAL
    )

    assert decision.spec is None


def test_unknown_tool_denied_for_read_only_task():
    policy = ToolPolicy(
        default_tool_registry(),
        strict_registry=False,
    )

    decision = policy.authorize(
        context(
            tool_name=(
                "dynamic_plugin_tool"
            ),

            workspace_access=(
                "read_only"
            ),
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.DENY
    )


def test_unknown_tool_denied_in_strict_mode():
    policy = ToolPolicy(
        default_tool_registry(),
        strict_registry=True,
    )

    decision = policy.authorize(
        context(
            tool_name=(
                "dynamic_plugin_tool"
            )
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.DENY
    )


def test_wrong_workdir_scope_is_denied():
    spec = ToolSpec(
        name="session_only",

        executors=frozenset(
            {"claude"}
        ),

        workdir_scopes=frozenset(
            {"session"}
        ),

        access=(
            ToolAccess.READ_ONLY
        ),

        side_effect=(
            ToolSideEffect.NONE
        ),

        idempotency=(
            ToolIdempotency.SAFE
        ),
    )

    policy = ToolPolicy(
        ToolRegistry(
            [spec]
        )
    )

    decision = policy.authorize(
        context(
            tool_name=(
                "session_only"
            ),

            workdir_scope=(
                "repository"
            ),
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.DENY
    )


def test_denied_config_has_priority():
    registry = (
        default_tool_registry()
    )

    policy = ToolPolicy(
        registry,

        allowed_by_executor={
            "claude": [
                "*",
            ]
        },

        denied_by_executor={
            "claude": [
                "Read",
            ]
        },
    )

    decision = policy.authorize(
        context(
            tool_name="Read"
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.DENY
    )


def test_allow_list_rejects_unlisted_tool():
    policy = ToolPolicy(
        default_tool_registry(),

        allowed_by_executor={
            "claude": [
                "Read",
            ]
        },
    )

    decision = policy.authorize(
        context(
            tool_name="Glob"
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.DENY
    )


def test_allow_list_accepts_alias():
    policy = ToolPolicy(
        default_tool_registry(),

        allowed_by_executor={
            "claude": [
                "read_file",
            ]
        },
    )

    decision = policy.authorize(
        context(
            tool_name="Read"
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.ALLOW
    )


def test_hook_can_block_tool():
    hooks = HookChain(
        [
            HookRule(
                name="block-read",

                phase="before_tool",

                action="block",

                tool_names=(
                    "Read",
                ),

                message=(
                    "Read blocked "
                    "for test"
                ),
            )
        ]
    )

    policy = ToolPolicy(
        default_tool_registry(),
        hook_chain=hooks,
    )

    decision = policy.authorize(
        context(
            tool_name="Read"
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.DENY
    )

    assert (
        "Read blocked"
        in decision.reason
    )


def test_hook_warning_is_preserved():
    hooks = HookChain(
        [
            HookRule(
                name="warn-read",

                phase="before_tool",

                action="warn",

                tool_names=(
                    "Read",
                ),

                message=(
                    "read warning"
                ),
            )
        ]
    )

    policy = ToolPolicy(
        default_tool_registry(),
        hook_chain=hooks,
    )

    decision = policy.authorize(
        context(
            tool_name="Read"
        )
    )

    assert (
        decision.action
        == ToolPolicyAction.ALLOW
    )

    assert decision.warnings

    assert (
        "read warning"
        in decision.warnings[0]
    )


def test_external_write_requires_approval_even_if_spec_says_never():
    spec = ToolSpec(
        name="create_ticket",

        executors=frozenset(
            {"claude"}
        ),

        access=ToolAccess.WRITE,

        side_effect=(
            ToolSideEffect
            .EXTERNAL_WRITE
        ),

        idempotency=(
            ToolIdempotency
            .IDEMPOTENT
        ),

        approval=(
            ApprovalRequirement.NEVER
        ),
    )

    policy = ToolPolicy(
        ToolRegistry(
            [spec]
        )
    )

    decision = policy.authorize(
        context(
            tool_name=(
                "create_ticket"
            )
        )
    )

    assert (
        decision.action
        ==
        ToolPolicyAction
        .REQUIRE_APPROVAL
    )


def test_non_idempotent_write_requires_approval():
    spec = ToolSpec(
        name="send_email",

        executors=frozenset(
            {"claude"}
        ),

        access=ToolAccess.WRITE,

        side_effect=(
            ToolSideEffect
            .EXTERNAL_WRITE
        ),

        idempotency=(
            ToolIdempotency
            .NON_IDEMPOTENT
        ),

        approval=(
            ApprovalRequirement.NEVER
        ),
    )

    policy = ToolPolicy(
        ToolRegistry(
            [spec]
        )
    )

    decision = policy.authorize(
        context(
            tool_name="send_email"
        )
    )

    assert (
        decision.action
        ==
        ToolPolicyAction
        .REQUIRE_APPROVAL
    )