import pytest

from tasker.domain.tool import (
    ToolAccess,
    ToolSideEffect,
    ToolSpec,
)

from tasker.tool_registry import (
    ToolRegistry,
    default_tool_registry,
)


def test_register_and_resolve():
    registry = ToolRegistry()

    spec = ToolSpec(
        name="hello",
        description="hello tool",
    )

    registry.register(spec)

    assert (
        registry.resolve(
            "hello"
        )
        == spec
    )


def test_resolve_is_case_insensitive():
    registry = ToolRegistry(
        [
            ToolSpec(
                name="Read",
            )
        ]
    )

    assert (
        registry.resolve(
            "READ"
        ).name
        == "Read"
    )


def test_alias_resolves_to_canonical_tool():
    registry = ToolRegistry(
        [
            ToolSpec(
                name="Read",
                aliases=(
                    "read_file",
                ),
            )
        ]
    )

    resolved = registry.resolve(
        "read_file"
    )

    assert resolved is not None

    assert (
        resolved.name
        == "Read"
    )


def test_duplicate_name_rejected():
    registry = ToolRegistry(
        [
            ToolSpec(
                name="Read"
            )
        ]
    )

    with pytest.raises(
        ValueError
    ):
        registry.register(
            ToolSpec(
                name="read"
            )
        )


def test_alias_collision_rejected():
    registry = ToolRegistry(
        [
            ToolSpec(
                name="Read",
                aliases=(
                    "file",
                ),
            )
        ]
    )

    with pytest.raises(
        ValueError
    ):
        registry.register(
            ToolSpec(
                name="Write",
                aliases=(
                    "file",
                ),
                access=(
                    ToolAccess.WRITE
                ),
                side_effect=(
                    ToolSideEffect
                    .LOCAL_WRITE
                ),
            )
        )


def test_replace_tool_updates_aliases():
    registry = ToolRegistry(
        [
            ToolSpec(
                name="Read",
                aliases=(
                    "old_alias",
                ),
            )
        ]
    )

    registry.register(
        ToolSpec(
            name="Read",
            aliases=(
                "new_alias",
            ),
        ),
        replace=True,
    )

    assert (
        registry.resolve(
            "old_alias"
        )
        is None
    )

    assert (
        registry.resolve(
            "new_alias"
        )
        is not None
    )


def test_unregister_by_alias():
    registry = ToolRegistry(
        [
            ToolSpec(
                name="Read",
                aliases=(
                    "read_file",
                ),
            )
        ]
    )

    assert registry.unregister(
        "read_file"
    )

    assert (
        registry.resolve(
            "Read"
        )
        is None
    )


def test_search_by_name():
    registry = default_tool_registry()

    result = registry.search(
        "Read",
        executor="claude",
    )

    assert result

    assert (
        result[0].name
        == "Read"
    )


def test_search_filters_executor_capability():
    registry = default_tool_registry()

    result = registry.search(
        "command",
        executor="claude",
    )

    names = {
        tool.name
        for tool in result
    }

    assert (
        "run_command"
        not in names
    )


def test_registry_does_not_apply_task_policy():
    registry = default_tool_registry()

    spec = registry.resolve(
        "Bash"
    )

    assert spec is not None


    assert (
        spec.name
        == "Bash"
    )