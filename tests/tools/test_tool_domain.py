import pytest

from tasker.domain.tool import (
    ApprovalRequirement,
    ToolAccess,
    ToolExecutionMode,
    ToolIdempotency,
    ToolSideEffect,
    ToolSpec,
)


def test_read_tool_spec():
    spec = ToolSpec(
        name="Read",

        access=(
            ToolAccess.READ_ONLY
        ),

        side_effect=(
            ToolSideEffect.NONE
        ),

        idempotency=(
            ToolIdempotency.SAFE
        ),

        approval=(
            ApprovalRequirement.NEVER
        ),
    )

    assert spec.name == "Read"

    assert (
        spec.execution_mode
        == ToolExecutionMode
        .BACKEND_NATIVE
    )


def test_read_only_tool_cannot_have_write_side_effect():
    with pytest.raises(
        ValueError
    ):
        ToolSpec(
            name="broken",

            access=(
                ToolAccess.READ_ONLY
            ),

            side_effect=(
                ToolSideEffect.LOCAL_WRITE
            ),
        )


def test_side_effect_free_tool_cannot_be_non_idempotent():
    with pytest.raises(
        ValueError
    ):
        ToolSpec(
            name="broken",

            side_effect=(
                ToolSideEffect.NONE
            ),

            idempotency=(
                ToolIdempotency
                .NON_IDEMPOTENT
            ),
        )


def test_names_contains_aliases():
    spec = ToolSpec(
        name="Read",
        aliases=(
            "read_file",
            "cat_file",
        ),
    )

    assert spec.names == (
        "Read",
        "read_file",
        "cat_file",
    )

def test_aliases_cannot_be_plain_string():
    with pytest.raises(TypeError):
        ToolSpec(
            name="Read",
            aliases="read_file",
        )

def test_single_alias_tuple_is_valid():
    spec=ToolSpec(
        name="Read",
        aliases=(
            "read_file",
        ),
    )
    assert spec.aliases == (
        "read_file",
    )
    assert spec.names == (
        "Read",
        "read_file",
    )