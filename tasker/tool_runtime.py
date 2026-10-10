from __future__ import annotations

import hashlib
import json
import secrets
import time

from dataclasses import dataclass
from typing import Any

from .artifact_store import (
    ArtifactStore,
)

from .domain.artifact import (
    ArtifactKind,
)

from .domain.tool import (
    ToolExecutionMode,
    ToolIdempotency,
    ToolSideEffect,
    ToolSpec,
)

from .domain.tool_call import (
    ToolCallStatus,
    ToolInvocation,
    ToolResult,
)

from .tool_adapter import (
    McpToolAdapter,
    LocalToolAdapter,
    ToolAdapter,
    ToolOutcomeUnknown,
    ToolTransientError,
)

from .tool_journal import (
    ToolJournal,
)

from .tool_policy import (
    ToolPolicy,
    ToolPolicyAction,
    ToolPolicyContext,
    ToolPolicyDecision,
)

from .tool_validation import (
    ToolValidationError,
    validate_tool_input,
)


@dataclass(frozen=True)
class PreparedToolCall:

    invocation: ToolInvocation

    spec: ToolSpec | None

    decision: ToolPolicyDecision


class ToolRuntime:

    def __init__(
        self,
        *,
        session_id: str,
        registry,
        policy: ToolPolicy,
        journal:
            ToolJournal | None = None,
        artifact_store:
            ArtifactStore | None = None,

        local_adapter:
            LocalToolAdapter | None = None,

        mcp_adapter:
            McpToolAdapter | None = None,

        max_retries: int = 1,

        initial_delay: float = 0.25,

        max_delay: float = 2.0,

        result_max_chars: int = 4000,

        artifact_threshold_chars:
            int = 3000,

        artifact_preview_chars:
            int = 800,
    ) -> None:

        self.session_id = (
            session_id
        )

        self.registry = registry

        self.policy = policy

        self.journal = journal

        self.artifact_store = (
            artifact_store
        )

        self.local_adapter = (
            local_adapter
        )

        self.mcp_adapter = (
            mcp_adapter
        )

        self.max_retries = max(
            0,
            int(max_retries),
        )

        self.initial_delay = max(
            0.0,
            float(initial_delay),
        )

        self.max_delay = max(
            self.initial_delay,
            float(max_delay),
        )

        self.result_max_chars = max(
            200,
            int(result_max_chars),
        )

        self.artifact_threshold_chars = max(
            500,
            int(
                artifact_threshold_chars
            ),
        )

        self.artifact_preview_chars = max(
            100,
            int(
                artifact_preview_chars
            ),
        )

    def _invocation_id(
        self,
        *,
        context: ToolPolicyContext,
        canonical_name: str,
        external_call_id: str = "",
        idempotency_key: str = "",
    ) -> str:

        if external_call_id:
            unique = (
                "external:"
                + external_call_id
            )

        elif idempotency_key:
            unique = (
                "idempotency:"
                + idempotency_key
            )

        else:
            unique = (
                "nonce:"
                + secrets.token_hex(
                    16
                )
            )

        raw = (
            f"{self.session_id}|"
            f"{context.task_id}|"
            f"{context.attempt_id}|"
            f"{context.executor}|"
            f"{canonical_name}|"
            f"{unique}"
        )

        digest = hashlib.sha256(
            raw.encode(
                "utf-8"
            )
        ).hexdigest()[:24]

        return (
            f"tc-{digest}"
        )


    def prepare(
        self,
        context: ToolPolicyContext,
        *,
        external_call_id: str = "",
        idempotency_key: str = "",
    ) -> PreparedToolCall:

        decision = (
            self.policy.authorize(
                context
            )
        )

        spec = decision.spec

        canonical_name = (
            decision.canonical_name
            or context.tool_name
        )

        if (
            decision.allowed
            and spec is not None
            and spec.input_schema
        ):

            try:

                validate_tool_input(
                    context.input_data,
                    spec.input_schema,
                )

            except ToolValidationError as exc:
                decision = (
                    ToolPolicyDecision(
                        action=(
                            ToolPolicyAction
                            .DENY
                        ),
                        reason=(
                            "invalid tool input: "
                            f"{exc}"
                        ),
                        spec=spec,
                        canonical_name=(
                            canonical_name
                        ),
                    )
                )

        invocation = ToolInvocation(
            invocation_id=(
                self._invocation_id(
                    context=context,
                    canonical_name=(
                        canonical_name
                    ),
                    external_call_id=(
                        external_call_id
                    ),
                    idempotency_key=(
                        idempotency_key
                    ),
                )
            ),

            session_id=(
                self.session_id
            ),

            task_id=(
                context.task_id
            ),

            attempt_id=(
                context.attempt_id
            ),

            executor=(
                context.executor
            ),

            tool_name=(
                context.tool_name
            ),

            canonical_name=(
                canonical_name
            ),

            input_data=dict(
                context.input_data
            ),

            workdir=(
                context.workdir
            ),

            workspace_access=(
                context.workspace_access
            ),

            workdir_scope=(
                context.workdir_scope
            ),

            idempotency_key=(
                idempotency_key
            ),
        )

        if self.journal is not None:

            self.journal.start(
                invocation=invocation,
                policy_action=(
                    decision.action.value
                ),
                policy_reason=(
                    decision.reason
                ),
                side_effect=(
                    spec.side_effect.value
                    if spec is not None
                    else
                    ToolSideEffect
                    .UNKNOWN.value
                ),
                idempotency=(
                    spec.idempotency.value
                    if spec is not None
                    else
                    ToolIdempotency
                    .UNKNOWN.value
                ),
                status=(
                    ToolCallStatus.DENIED

                    if decision.action
                    == ToolPolicyAction
                    .DENY

                    else
                    ToolCallStatus
                    .REQUESTED
                ),
            )

        return PreparedToolCall(
            invocation=invocation,

            spec=spec,

            decision=decision,
        )

    def record_approval(
        self,
        prepared: PreparedToolCall,
        *,
        allowed: bool,
    ) -> None:

        if self.journal is None:
            return

        self.journal.transition(
            prepared
            .invocation
            .invocation_id,
            status=(
                ToolCallStatus.APPROVED
                if allowed
                else
                ToolCallStatus.DENIED
            ),
        )


    def _adapter_for(
        self,
        spec: ToolSpec,
    ) -> ToolAdapter:

        if (
            spec.execution_mode
            == ToolExecutionMode
            .TASKER_LOCAL
        ):

            if self.local_adapter is None:

                raise RuntimeError(
                    "local tool adapter "
                    "not configured"
                )

            return self.local_adapter

        if (
            spec.execution_mode
            == ToolExecutionMode.MCP
        ):

            if self.mcp_adapter is None:

                raise RuntimeError(
                    "MCP adapter "
                    "not configured"
                )

            return self.mcp_adapter

        raise RuntimeError(
            "backend-native tool must "
            "be executed by its backend"
        )


    @staticmethod
    def _retry_allowed(
        spec: ToolSpec,
        invocation: ToolInvocation,
    ) -> bool:

        if (
            spec.idempotency
            == ToolIdempotency.SAFE
        ):
            return True

        if (
            spec.idempotency
            == ToolIdempotency
            .IDEMPOTENT
            and invocation
            .idempotency_key
        ):
            return True

        return False

    def _retry_delay(
        self,
        retry_no: int,
    ) -> float:

        return min(
            self.max_delay,

            self.initial_delay
            * (
                2
                ** max(
                    0,
                    retry_no - 1,
                )
            ),
        )


    @staticmethod
    def _stringify(
        value: Any,
    ) -> str:

        if isinstance(
            value,
            str,
        ):
            return value

        return json.dumps(
            value,
            ensure_ascii=False,
            default=str,
        )

    def _result(
        self,
        prepared: PreparedToolCall,
        *,
        status: ToolCallStatus,
        output: str = "",
        error: str = "",
        metadata:
            dict | None = None,
        retryable: bool = False,
        started_at: float = 0.0,
    ) -> ToolResult:

        invocation = (
            prepared.invocation
        )

        artifact_ids: tuple[
            str,
            ...
        ] = ()

        rendered_output = (
            output
            or ""
        )

        metadata = dict(
            metadata
            or {}
        )

        if (
            rendered_output
            and self.artifact_store
            is not None
            and len(rendered_output)
            >
            self.artifact_threshold_chars
        ):

            artifact = (
                self.artifact_store
                .save_text(
                    task_id=(
                        invocation
                        .task_id
                    ),

                    name=(
                        f"{invocation"
                        ".canonical_name}-"
                        f"{invocation"
                        ".invocation_id}.txt"
                    ),

                    content=(
                        rendered_output
                    ),

                    kind=(
                        ArtifactKind
                        .TOOL_OUTPUT
                    ),
                )
            )

            artifact_ids = (
                artifact.id,
            )

            metadata[
                "artifact_path"
            ] = (
                artifact
                .relative_path
            )

            metadata[
                "full_output_chars"
            ] = len(
                rendered_output
            )

            rendered_output = (
                rendered_output[
                    :self
                    .artifact_preview_chars
                ]
                +
                "\n...[Tool Output "
                "已保存为 Artifact]..."
            )

        elif (
            len(rendered_output)
            >
            self.result_max_chars
        ):

            rendered_output = (
                rendered_output[
                    :self
                    .result_max_chars
                ]
                +
                "\n...[Tool Result "
                "截断]..."
            )

        return ToolResult(
            invocation_id=(
                invocation
                .invocation_id
            ),
            status=status,
            output=(
                rendered_output
            ),
            error=error,
            metadata=metadata,
            artifact_ids=(
                artifact_ids
            ),
            retryable=(
                retryable
            ),
            started_at=(
                started_at
            ),
            ended_at=time.time(),
        )

    def _persist_result(
        self,
        result: ToolResult,
    ) -> ToolResult:

        if self.journal is not None:
            self.journal.transition(
                result.invocation_id,
                status=(
                    result.status
                ),
                result=result,
            )

        return result


    def execute(
        self,
        prepared: PreparedToolCall,
        *,
        approval_granted:
            bool = False,
    ) -> ToolResult:

        decision = (
            prepared.decision
        )
        invocation = (
            prepared.invocation
        )
        spec = prepared.spec
        if (
            decision.action
            == ToolPolicyAction.DENY
        ):
            return self._persist_result(
                self._result(
                    prepared,
                    status=(
                        ToolCallStatus
                        .DENIED
                    ),
                    error=(
                        decision.reason
                    ),
                )
            )

        if (
            decision.requires_approval
            and not approval_granted
        ):

            return self._persist_result(
                self._result(
                    prepared,
                    status=(
                        ToolCallStatus
                        .DENIED
                    ),
                    error=(
                        "approval required"
                    ),
                )
            )

        if spec is None:
            return self._persist_result(
                self._result(
                    prepared,
                    status=(
                        ToolCallStatus
                        .FAILED
                    ),
                    error=(
                        "unregistered tool "
                        "cannot be executed "
                        "by Tasker runtime"
                    ),
                )
            )

        if (
            spec.execution_mode
            == ToolExecutionMode
            .BACKEND_NATIVE
        ):

            raise RuntimeError(
                "backend-native tools "
                "must be executed by "
                "Claude/Codex"
            )

        # IDEMPOTENT 写操作：同 key 成功过则复用结果。
        # SAFE 不做缓存，因为读取结果可能随时间变化。
        if (
            spec.idempotency
            == ToolIdempotency
            .IDEMPOTENT
            and invocation
            .idempotency_key
            and self.journal
            is not None
        ):

            existing = (
                self.journal
                .find_success_by_idempotency(
                    canonical_name=(
                        invocation
                        .canonical_name
                    ),

                    idempotency_key=(
                        invocation
                        .idempotency_key
                    ),
                )
            )

            if (
                existing is not None
                and existing.result
                is not None
            ):

                return (
                    existing.result
                )

        adapter = self._adapter_for(
            spec
        )

        if self.journal is not None:

            self.journal.transition(
                invocation
                .invocation_id,
                status=(
                    ToolCallStatus
                    .RUNNING
                ),
            )

        started_at = time.time()

        retry_allowed = (
            self._retry_allowed(
                spec,
                invocation,
            )
        )

        for attempt_no in range(
            self.max_retries
            + 1
        ):

            try:

                raw = adapter.execute(
                    spec,
                    invocation,
                )
                result = self._result(
                    prepared,
                    status=(
                        ToolCallStatus
                        .SUCCESS
                    ),
                    output=(
                        self._stringify(
                            raw
                        )
                    ),
                    metadata={
                        "attempt_no":
                            attempt_no
                            + 1,
                    },
                    started_at=(
                        started_at
                    ),
                )

                return (
                    self._persist_result(
                        result
                    )
                )

            except ToolOutcomeUnknown as exc:

                result = self._result(
                    prepared,
                    status=(
                        ToolCallStatus
                        .UNKNOWN
                    ),
                    error=str(exc),
                    retryable=False,
                    started_at=(
                        started_at
                    ),
                )

                return (
                    self._persist_result(
                        result
                    )
                )

            except (
                ToolTransientError,
                TimeoutError,
                ConnectionError,
            ) as exc:

                has_retry = (
                    retry_allowed
                    and attempt_no
                    < self.max_retries
                )

                if has_retry:
                    time.sleep(
                        self._retry_delay(
                            attempt_no
                            + 1
                        )
                    )
                    continue

                result = self._result(
                    prepared,
                    status=(
                        ToolCallStatus
                        .FAILED
                    ),
                    error=str(exc),
                    retryable=(
                        retry_allowed
                    ),
                    started_at=(
                        started_at
                    ),
                )

                return (
                    self._persist_result(
                        result
                    )
                )

            except Exception as exc:

                result = self._result(
                    prepared,
                    status=(
                        ToolCallStatus
                        .FAILED
                    ),
                    error=str(exc),
                    retryable=False,
                    started_at=(
                        started_at
                    ),
                )

                return (
                    self._persist_result(
                        result
                    )
                )

        raise AssertionError(
            "unreachable"
        )


    def complete_backend(
        self,
        prepared: PreparedToolCall,
        *,
        output: Any = "",
        is_error: bool = False,
        metadata:
            dict | None = None,
    ) -> ToolResult:

        result = self._result(
            prepared,
            status=(
                ToolCallStatus.FAILED
                if is_error
                else
                ToolCallStatus.SUCCESS
            ),
            output=(
                self._stringify(
                    output
                )
            ),
            error=(
                self._stringify(
                    output
                )
                if is_error
                else ""
            ),
            metadata=metadata,
            retryable=False,
            started_at=(
                prepared
                .invocation
                .created_at
            ),
        )

        return self._persist_result(
            result
        )

    def mark_unknown(
        self,
        prepared: PreparedToolCall,
        *,
        error: str,
    ) -> ToolResult:

        return self._persist_result(
            self._result(
                prepared,
                status=(
                    ToolCallStatus.UNKNOWN
                ),
                error=error,
                retryable=False,
                started_at=(
                    prepared
                    .invocation
                    .created_at
                ),
            )
        )