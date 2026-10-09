from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolAccess(str, Enum):
    """
    工具对工作区的基本访问能力。
    """
    READ_ONLY = "read_only"
    WRITE = "write"


class ToolSideEffect(str, Enum):
    """
    工具可能产生的副作用类型。

    NONE:
        纯读取。

    LOCAL_WRITE:
        修改本地 workspace / repository。

    EXTERNAL_WRITE:
        修改外部系统，例如数据库、GitHub、工单系统。

    UNKNOWN:
        无法可靠判断。
    """

    NONE = "none"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    UNKNOWN = "unknown"


class ToolIdempotency(str, Enum):
    """
    工具调用的幂等属性。
    """

    SAFE = "safe"

    IDEMPOTENT = "idempotent"

    NON_IDEMPOTENT = "non_idempotent"

    UNKNOWN = "unknown"


class ToolExecutionMode(str, Enum):
    """
    谁真正负责执行工具。
    """

    BACKEND_NATIVE = "backend_native"

    TASKER_LOCAL = "tasker_local"

    MCP = "mcp"


class ApprovalRequirement(str, Enum):
    """
    ToolSpec 自身对审批的需求。

    真正是否批准，仍由 ToolPolicy 决定。
    """

    NEVER = "never"
    REQUIRED = "required"


@dataclass(frozen=True)
class ToolSpec:
    """
    一个工具的静态能力定义。

    注意：
    ToolSpec 描述的是“工具是什么”，
    不是“当前 Task 能不能调用”。
    """

    name: str

    description: str = ""

    aliases: tuple[str, ...] = ()

    executors: frozenset[str] = field(
        default_factory=lambda:
            frozenset(
                {
                    "claude",
                    "codex",
                }
            )
    )

    workdir_scopes: frozenset[str] = field(
        default_factory=lambda:
            frozenset(
                {
                    "session",
                    "repository",
                }
            )
    )

    access: ToolAccess = (
        ToolAccess.READ_ONLY
    )

    side_effect: ToolSideEffect = (
        ToolSideEffect.NONE
    )

    idempotency: ToolIdempotency = (
        ToolIdempotency.SAFE
    )

    approval: ApprovalRequirement = (
        ApprovalRequirement.NEVER
    )

    execution_mode: ToolExecutionMode = (
        ToolExecutionMode.BACKEND_NATIVE
    )

    source: str = "builtin"

    input_schema: dict[str, Any] = field(
        default_factory=dict
    )

    output_schema: dict[str, Any] = field(
        default_factory=dict
    )

    @property
    def names(
        self,
    ) -> tuple[str, ...]:
        return (
            self.name,
            *self.aliases,
        )

    def __post_init__(
        self,
    ) -> None:

        if not self.name.strip():
            raise ValueError(
                "tool name cannot be empty"
            )

        if not self.executors:
            raise ValueError(
                f"tool {self.name} "
                "requires executors"
            )

        allowed_executors = {
            "claude",
            "codex",
            "tasker",
        }

        if not self.executors.issubset(
            allowed_executors
        ):
            raise ValueError(
                f"tool {self.name} "
                "contains invalid executor"
            )

        allowed_scopes = {
            "session",
            "repository",
        }

        if not self.workdir_scopes:
            raise ValueError(
                f"tool {self.name} "
                "requires workdir scope"
            )

        if not self.workdir_scopes.issubset(
            allowed_scopes
        ):
            raise ValueError(
                f"tool {self.name} "
                "contains invalid "
                "workdir scope"
            )

        if (
            self.access
            == ToolAccess.READ_ONLY
            and self.side_effect
            in {
                ToolSideEffect.LOCAL_WRITE,
                ToolSideEffect.EXTERNAL_WRITE,
            }
        ):
            raise ValueError(
                f"read-only tool "
                f"{self.name} cannot "
                "declare write side effect"
            )

        if (
            self.side_effect
            == ToolSideEffect.NONE
            and self.idempotency
            == ToolIdempotency.NON_IDEMPOTENT
        ):
            raise ValueError(
                f"side-effect-free tool "
                f"{self.name} cannot be "
                "non-idempotent"
            )