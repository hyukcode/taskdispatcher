from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .domain.tool import (
    ApprovalRequirement,
    ToolAccess,
    ToolIdempotency,
    ToolSideEffect,
    ToolSpec,
)

from .policy_hooks import (
    HookChain,
    HookContext,
)

from .tool_registry import (
    ToolRegistry,
)


class ToolPolicyAction(
    str,
    Enum,
):
    """
    ToolPolicy 的统一授权结果。

    ALLOW: 编排器策略允许直接执行。

    DENY: 明确禁止。

    REQUIRE_APPROVAL: 策略没有直接拒绝，但必须进入 ApprovalBroker。
    """

    ALLOW = "allow"

    DENY = "deny"

    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class ToolPolicyContext:
    """
    一次具体 Tool 调用的动态上下文。

    ToolSpec 描述：“工具是什么”

    ToolPolicyContext 描述：“谁在什么情况下想调用它”
    """

    executor: str

    task_id: str

    attempt_id: str

    tool_name: str

    input_data: dict

    workspace_access: str

    workdir_scope: str

    workdir: str


@dataclass(frozen=True)
class ToolPolicyDecision:
    """
    ToolPolicy 的最终决策。
    """

    action: ToolPolicyAction

    reason: str

    # Registry 能解析时存在。
    spec: ToolSpec | None = None

    # canonical tool name。
    canonical_name: str = ""

    # Hook 产生的非阻断提示。
    warnings: tuple[str,...] = ()

    @property
    def allowed(
        self,
    ) -> bool:

        return  self.action != ToolPolicyAction.DENY


    @property
    def requires_approval(
        self,
    ) -> bool:

        return  self.action == ToolPolicyAction.REQUIRE_APPROVAL


def _normalized_names(
    values: Iterable[str],
) -> frozenset[str]:

    return frozenset(str(value).strip().casefold() for value in values if str(value).strip())


class ToolPolicy:
    """
    Tool 调用统一授权层。

    负责：

        Registry lookup
        executor capability
        workspace_access
        workdir_scope
        allow/deny config
        Hook before_tool
        side-effect risk
        idempotency risk
        approval requirement

    不负责：

        等待用户审批
        执行 Tool
        Tool result
        Retry
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        hook_chain: HookChain | None = None,
        allowed_by_executor:
            dict[
                str,
                Iterable[str],
            ]
            | None = None,
        denied_by_executor:
            dict[
                str,
                Iterable[str],
            ]
            | None = None,
        strict_registry: bool = False,
    ) -> None:

        self.registry = registry

        self.hook_chain = (
            hook_chain
            or HookChain()
        )

        self.strict_registry = (
            strict_registry
        )

        self._allowed = {
            self._normalize_executor(
                executor
            ):
            _normalized_names(
                values
            )
            for executor, values
            in (
                allowed_by_executor
                or {}
            ).items()
        }

        self._denied = {
            self._normalize_executor(
                executor
            ):
            _normalized_names(
                values
            )
            for executor, values
            in (
                denied_by_executor
                or {}
            ).items()
        }


    @classmethod
    def from_config(
        cls,
        cfg,
        *,
        registry: ToolRegistry,
        hook_chain: HookChain | None = None,
        strict_registry: bool = False,
    ) -> "ToolPolicy":

        claude = getattr(
            cfg,
            "claude",
            None,
        )

        allowed = {
            "claude": getattr(
                claude,
                "allowed_tools",
                (),
            )
        }

        denied = {
            "claude": getattr(
                claude,
                "disallowed_tools",
                (),
            )
        }

        return cls(
            registry,

            hook_chain=hook_chain,

            allowed_by_executor=(
                allowed
            ),

            denied_by_executor=(
                denied
            ),

            strict_registry=(
                strict_registry
            ),
        )

    # ================================================================
    # normalization
    # ================================================================

    @staticmethod
    def _normalize_name(
        value: str,
    ) -> str:

        return str(
            value or ""
        ).strip().casefold()

    @staticmethod
    def _normalize_executor(
        value: str,
    ) -> str:

        return str(
            value or ""
        ).strip().casefold()

    # ================================================================
    # result helpers
    # ================================================================

    @staticmethod
    def _allow(
        *,
        reason: str,
        spec: ToolSpec | None,
        canonical_name: str,
        warnings:
            tuple[str, ...] = (),
    ) -> ToolPolicyDecision:

        return ToolPolicyDecision(
            action=(
                ToolPolicyAction.ALLOW
            ),

            reason=reason,

            spec=spec,

            canonical_name=(
                canonical_name
            ),

            warnings=warnings,
        )

    @staticmethod
    def _deny(
        *,
        reason: str,
        spec: ToolSpec | None = None,
        canonical_name: str = "",
        warnings:
            tuple[str, ...] = (),
    ) -> ToolPolicyDecision:

        return ToolPolicyDecision(
            action=(
                ToolPolicyAction.DENY
            ),

            reason=reason,

            spec=spec,

            canonical_name=(
                canonical_name
            ),

            warnings=warnings,
        )

    @staticmethod
    def _require_approval(
        *,
        reason: str,
        spec: ToolSpec | None,
        canonical_name: str,
        warnings:
            tuple[str, ...] = (),
    ) -> ToolPolicyDecision:

        return ToolPolicyDecision(
            action=(
                ToolPolicyAction
                .REQUIRE_APPROVAL
            ),

            reason=reason,

            spec=spec,

            canonical_name=(
                canonical_name
            ),

            warnings=warnings,
        )

    # ================================================================
    # configured allow / deny
    # ================================================================

    def _configured_decision(
        self,
        *,
        executor: str,
        names: set[str],
    ) -> tuple[
        bool,
        str,
    ]:
        """
        检查现有 Config 的 allow/deny。

        返回：

            (
                是否通过配置检查,
                原因,
            )

        deny 优先级高于 allow。
        """

        executor = (
            self._normalize_executor(
                executor
            )
        )

        denied = self._denied.get(
            executor,
            frozenset(),
        )

        if (
            "*" in denied
            or names.intersection(
                denied
            )
        ):
            return (
                False,
                "tool denied by "
                f"{executor} configuration",
            )

        allowed = self._allowed.get(
            executor,
            frozenset(),
        )

        # allow list 为空：
        # 没有额外白名单限制。
        if not allowed:
            return (
                True,
                "",
            )

        if "*" in allowed:
            return (
                True,
                "",
            )

        if names.intersection(
            allowed
        ):
            return (
                True,
                "",
            )

        return (
            False,
            "tool is not included in "
            f"{executor} allow list",
        )

    # ================================================================
    # hooks
    # ================================================================

    def _before_hook(
        self,
        context: ToolPolicyContext,
        *,
        canonical_name: str,
    ):
        """
        调用现有 HookChain。

        这里暂时复用 HookChain，
        后续 Phase 5 可以进一步把 Hook
        变成 ToolPolicy Rule。
        """

        return self.hook_chain.before_tool(
            HookContext(
                executor=context.executor,

                task_id=context.task_id,

                attempt_id=(
                    context.attempt_id
                ),

                tool_name=(
                    canonical_name
                    or context.tool_name
                ),

                input_data=(
                    context.input_data
                    if isinstance(
                        context.input_data,
                        dict,
                    )
                    else {}
                ),

                workdir=context.workdir,
            )
        )

    # ================================================================
    # authorize
    # ================================================================

    def authorize(
        self,
        context: ToolPolicyContext,
    ) -> ToolPolicyDecision:
        """
        对一次 Tool Invocation 做完整策略判断。
        """

        raw_name = (
            self._normalize_name(
                context.tool_name
            )
        )

        executor = (
            self._normalize_executor(
                context.executor
            )
        )

        # ------------------------------------------------------------
        # 1. 基础输入校验
        # ------------------------------------------------------------

        if not raw_name:

            return self._deny(
                reason=(
                    "tool name cannot "
                    "be empty"
                )
            )

        if not executor:

            return self._deny(
                reason=(
                    "executor cannot "
                    "be empty"
                )
            )

        # ------------------------------------------------------------
        # 2. Registry resolve
        #
        # executor namespace 已知，
        # 所以不会产生跨 backend ambiguity。
        # ------------------------------------------------------------

        spec = self.registry.resolve(
            context.tool_name,
            executor=executor,
        )

        # ------------------------------------------------------------
        # 3. Unknown tool
        # ------------------------------------------------------------

        if spec is None:

            names = {
                raw_name
            }

            configured, reason = (
                self._configured_decision(
                    executor=executor,
                    names=names,
                )
            )

            if not configured:

                return self._deny(
                    reason=reason,
                )

            # read-only Task 对未知工具
            # 采用 fail-closed。
            #
            # 因为我们无法证明这个未知工具
            # 没有写副作用。
            if (
                context.workspace_access
                == "read_only"
            ):

                return self._deny(
                    reason=(
                        "unknown tool cannot "
                        "be proven safe for "
                        "read-only task"
                    )
                )

            # Hook 即使对未知工具
            # 仍然有机会进行字符串/路径阻断。
            hook = self._before_hook(
                context,
                canonical_name=(
                    context.tool_name
                ),
            )

            if not hook.allowed:

                return self._deny(
                    reason=(
                        hook.message
                        or
                        "tool blocked by hook"
                    ),

                    canonical_name=(
                        context.tool_name
                    ),

                    warnings=(
                        hook.warnings
                    ),
                )

            if self.strict_registry:

                return self._deny(
                    reason=(
                        "tool is not registered"
                    ),

                    canonical_name=(
                        context.tool_name
                    ),

                    warnings=(
                        hook.warnings
                    ),
                )

            # 兼容当前 Claude/Codex
            # 动态工具。
            #
            # 不直接 ALLOW，
            # 而是要求审批。
            return self._require_approval(
                reason=(
                    "unregistered backend tool "
                    "requires approval"
                ),

                spec=None,

                canonical_name=(
                    context.tool_name
                ),

                warnings=(
                    hook.warnings
                ),
            )

        # ------------------------------------------------------------
        # 4. Canonical Tool name
        # ------------------------------------------------------------

        canonical_name = spec.name

        normalized_names = {
            self._normalize_name(
                name
            )
            for name
            in spec.names
        }

        # ------------------------------------------------------------
        # 5. workspace access
        # ------------------------------------------------------------

        if (
            context.workspace_access
            == "read_only"
            and
            spec.access
            == ToolAccess.WRITE
        ):

            return self._deny(
                reason=(
                    "read-only task cannot "
                    f"use write tool "
                    f"{canonical_name}"
                ),

                spec=spec,

                canonical_name=(
                    canonical_name
                ),
            )

        # ------------------------------------------------------------
        # 6. workdir scope
        # ------------------------------------------------------------

        if (
            context.workdir_scope
            not in spec.workdir_scopes
        ):

            return self._deny(
                reason=(
                    f"tool {canonical_name} "
                    "does not support "
                    f"workdir scope "
                    f"{context.workdir_scope}"
                ),

                spec=spec,

                canonical_name=(
                    canonical_name
                ),
            )

        # ------------------------------------------------------------
        # 7. executor allow/deny config
        # ------------------------------------------------------------

        configured, reason = (
            self._configured_decision(
                executor=executor,
                names=(
                    normalized_names
                ),
            )
        )

        if not configured:

            return self._deny(
                reason=reason,

                spec=spec,

                canonical_name=(
                    canonical_name
                ),
            )

        # ------------------------------------------------------------
        # 8. before_tool hooks
        # ------------------------------------------------------------

        hook = self._before_hook(
            context,
            canonical_name=(
                canonical_name
            ),
        )

        if not hook.allowed:

            return self._deny(
                reason=(
                    hook.message
                    or
                    "tool blocked by hook"
                ),

                spec=spec,

                canonical_name=(
                    canonical_name
                ),

                warnings=(
                    hook.warnings
                ),
            )

        # ------------------------------------------------------------
        # 9. Explicit approval requirement
        # ------------------------------------------------------------

        if (
            spec.approval
            ==
            ApprovalRequirement.REQUIRED
        ):

            return self._require_approval(
                reason=(
                    f"tool {canonical_name} "
                    "requires approval"
                ),

                spec=spec,

                canonical_name=(
                    canonical_name
                ),

                warnings=(
                    hook.warnings
                ),
            )

        # ------------------------------------------------------------
        # 10. Defensive side-effect policy
        #
        # 即使 ToolSpec 忘了声明 approval=required，
        # 高风险 side effect 仍然不能直接放行。
        # ------------------------------------------------------------

        if spec.side_effect in {
            ToolSideEffect
            .EXTERNAL_WRITE,

            ToolSideEffect
            .UNKNOWN,
        }:

            return self._require_approval(
                reason=(
                    f"tool {canonical_name} "
                    "has risky or unknown "
                    "side effects"
                ),

                spec=spec,

                canonical_name=(
                    canonical_name
                ),

                warnings=(
                    hook.warnings
                ),
            )

        # ------------------------------------------------------------
        # 11. Defensive idempotency policy
        # ------------------------------------------------------------

        if (
            spec.access
            == ToolAccess.WRITE
            and
            spec.idempotency
            in {
                ToolIdempotency
                .NON_IDEMPOTENT,

                ToolIdempotency
                .UNKNOWN,
            }
        ):

            return self._require_approval(
                reason=(
                    f"tool {canonical_name} "
                    "is not safely retryable"
                ),

                spec=spec,

                canonical_name=(
                    canonical_name
                ),

                warnings=(
                    hook.warnings
                ),
            )

        # ------------------------------------------------------------
        # 12. Safe
        # ------------------------------------------------------------

        return self._allow(
            reason=(
                f"tool {canonical_name} "
                "passed policy"
            ),

            spec=spec,

            canonical_name=(
                canonical_name
            ),

            warnings=(
                hook.warnings
            ),
        )