from __future__ import annotations

import time

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


TOOL_CALL_VERSION = 1


class ToolCallStatus(
    str,
    Enum,
):
    REQUESTED = "requested"
    APPROVED = "approved"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"

    # 最重要的：工具可能执行了，但我们无法确认。
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolInvocation:
    """
    一次真实 Tool 调用。

    ToolSpec: 工具是什么

    ToolInvocation: 谁在什么时候用什么参数调用它
    """
    invocation_id: str
    session_id: str
    task_id: str
    attempt_id: str
    executor: str
    tool_name: str
    canonical_name: str
    input_data: dict[str, Any]
    workdir: str
    workspace_access: str
    workdir_scope: str
    # 对 IDEMPOTENT Tool 很重要。
    idempotency_key: str = ""
    created_at: float = field(
        default_factory=time.time
    )


@dataclass(frozen=True)
class ToolResult:
    invocation_id: str
    status: ToolCallStatus
    output: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(
        default_factory=dict
    )
    artifact_ids: tuple[str, ...] = ()
    retryable: bool = False
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def success(
        self,
    ) -> bool:

        return (
            self.status
            == ToolCallStatus.SUCCESS
        )


@dataclass(frozen=True)
class ToolCallRecord:
    """
    Tool Journal 中持久化的数据。
    """
    version: int
    invocation: ToolInvocation
    status: ToolCallStatus
    policy_action: str
    policy_reason: str
    side_effect: str
    idempotency: str
    result: ToolResult | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


def invocation_to_dict(
    invocation: ToolInvocation,
) -> dict:

    return {
        "invocation_id": invocation.invocation_id,
        "session_id": invocation.session_id,
        "task_id": invocation.task_id,
        "attempt_id": invocation.attempt_id,
        "executor": invocation.executor,
        "tool_name": invocation.tool_name,
        "canonical_name": invocation.canonical_name,
        "input_data": invocation.input_data,
        "workdir": invocation.workdir,
        "workspace_access": invocation.workspace_access,
        "workdir_scope": invocation.workdir_scope,
        "idempotency_key": invocation.idempotency_key,
        "created_at": invocation.created_at,
    }


def invocation_from_dict(
    data: dict,
) -> ToolInvocation:

    return ToolInvocation(
        invocation_id=str(
            data.get(
                "invocation_id",
                "",
            )
        ),

        session_id=str(
            data.get(
                "session_id",
                "",
            )
        ),

        task_id=str(
            data.get(
                "task_id",
                "",
            )
        ),

        attempt_id=str(
            data.get(
                "attempt_id",
                "",
            )
        ),

        executor=str(
            data.get(
                "executor",
                "",
            )
        ),

        tool_name=str(
            data.get(
                "tool_name",
                "",
            )
        ),

        canonical_name=str(
            data.get(
                "canonical_name",
                "",
            )
        ),

        input_data=dict(
            data.get(
                "input_data",
                {},
            )
            or {}
        ),

        workdir=str(
            data.get(
                "workdir",
                "",
            )
        ),

        workspace_access=str(
            data.get(
                "workspace_access",
                "write",
            )
        ),

        workdir_scope=str(
            data.get(
                "workdir_scope",
                "session",
            )
        ),

        idempotency_key=str(
            data.get(
                "idempotency_key",
                "",
            )
        ),

        created_at=float(
            data.get(
                "created_at",
                0.0,
            )
            or 0.0
        ),
    )


def result_to_dict(
    result: ToolResult,
) -> dict:

    return {
        "invocation_id": result.invocation_id,
        "status": result.status.value,
        "output": result.output,
        "error": result.error,
        "metadata": result.metadata,
        "artifact_ids":
            list(
                result.artifact_ids
            ),
        "retryable": result.retryable,
        "started_at": result.started_at,
        "ended_at": result.ended_at,
    }


def result_from_dict(
    data: dict,
) -> ToolResult:

    return ToolResult(
        invocation_id=str(
            data.get(
                "invocation_id",
                "",
            )
        ),

        status=ToolCallStatus(
            str(
                data.get(
                    "status",
                    ToolCallStatus
                    .FAILED.value,
                )
            )
        ),

        output=str(
            data.get(
                "output",
                "",
            )
            or ""
        ),

        error=str(
            data.get(
                "error",
                "",
            )
            or ""
        ),

        metadata=dict(
            data.get(
                "metadata",
                {},
            )
            or {}
        ),

        artifact_ids=tuple(
            str(value)
            for value
            in (
                data.get(
                    "artifact_ids",
                    [],
                )
                or []
            )
        ),

        retryable=bool(
            data.get(
                "retryable",
                False,
            )
        ),

        started_at=float(
            data.get(
                "started_at",
                0.0,
            )
            or 0.0
        ),

        ended_at=float(
            data.get(
                "ended_at",
                0.0,
            )
            or 0.0
        ),
    )


def record_to_dict(
    record: ToolCallRecord,
) -> dict:

    return {
        "version": record.version,
        "invocation": invocation_to_dict(
                record.invocation
            ),
        "status": record.status.value,
        "policy_action": record.policy_action,
        "policy_reason": record.policy_reason,
        "side_effect": record.side_effect,
        "idempotency": record.idempotency,
        "result": (
            result_to_dict(
                record.result
            )
            if record.result
            is not None
            else None
        ),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def record_from_dict(
    data: dict,
) -> ToolCallRecord:

    version = int(
        data.get(
            "version",
            0,
        )
    )

    if version != TOOL_CALL_VERSION:

        raise ValueError(
            "unsupported tool call "
            f"version: {version}"
        )

    raw_result = data.get(
        "result"
    )

    return ToolCallRecord(
        version=version,

        invocation=(
            invocation_from_dict(
                dict(
                    data.get(
                        "invocation",
                        {},
                    )
                )
            )
        ),

        status=ToolCallStatus(
            str(
                data.get(
                    "status",
                    ToolCallStatus
                    .REQUESTED.value,
                )
            )
        ),

        policy_action=str(
            data.get(
                "policy_action",
                "",
            )
        ),

        policy_reason=str(
            data.get(
                "policy_reason",
                "",
            )
        ),

        side_effect=str(
            data.get(
                "side_effect",
                "unknown",
            )
        ),

        idempotency=str(
            data.get(
                "idempotency",
                "unknown",
            )
        ),

        result=(
            result_from_dict(
                raw_result
            )
            if isinstance(
                raw_result,
                dict,
            )
            else None
        ),

        created_at=float(
            data.get(
                "created_at",
                0.0,
            )
            or 0.0
        ),

        updated_at=float(
            data.get(
                "updated_at",
                0.0,
            )
            or 0.0
        ),
    )