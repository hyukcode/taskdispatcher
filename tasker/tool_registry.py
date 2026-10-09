from __future__ import annotations

from typing import Iterable

from .domain.tool import (
    ApprovalRequirement,
    ToolAccess,
    ToolExecutionMode,
    ToolIdempotency,
    ToolSideEffect,
    ToolSpec,
)


class AmbiguousToolError(
    LookupError
):
    pass


class ToolRegistry:

    def __init__(
        self,
        specs: Iterable[
            ToolSpec
        ] = (),
    ) -> None:

        # 真正注册的 ToolSpec，一个 ToolSpec 在这里仅保存一次。

        self._specs: list[
            ToolSpec
        ] = []

        # Secondary Index。
        # key:
        #     (executor, runtime_name)
        # value:
        #     ToolSpec
        # canonical name 和 aliases
        # 都进入这里。

        self._index: dict[
            tuple[str, str],
            ToolSpec,
        ] = {}

        self._version = 0

        for spec in specs:
            self.register(
                spec
            )

    @property
    def version(
        self,
    ) -> int:
        """
        用于：
            search cache invalidation
            runtime snapshot
            plugin hot reload
        """

        return self._version

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """
        当前所有 ToolSpec 的只读视图。
        """
        return tuple(self._specs)


    @staticmethod
    def _normalize(
        name: str,
    ) -> str:
        return str(name or "").strip().casefold()

    @staticmethod
    def _normalize_executor(
        executor: str,
    ) -> str:

        return str(executor or "").strip().casefold()

    def _keys_for(
        self,
        spec: ToolSpec,
    ) -> set[
        tuple[str, str]
    ]:

        names = {
            self._normalize(name)
            for name in spec.names
            if self._normalize(name)
        }

        keys: set[tuple[str, str]] = set()
        for executor in spec.executors:
            normalized_executor = (
                self._normalize_executor(
                    executor
                )
            )
            for name in names:
                keys.add(
                    (
                        normalized_executor,
                        name,
                    )
                )
        return keys

    def _same_identity(
        self,
        left: ToolSpec,
        right: ToolSpec,
    ) -> bool:

        return (
            self._normalize(
                left.name
            )
            ==
            self._normalize(
                right.name
            )
            and
            frozenset(
                self._normalize_executor(
                    executor
                )
                for executor
                in left.executors
            )
            ==
            frozenset(
                self._normalize_executor(
                    executor
                )
                for executor
                in right.executors
            )
        )

    def _build_index(
        self,
        specs: Iterable[ToolSpec],
    ) -> dict[tuple[str, str],ToolSpec]:
        # 从 ToolSpec 集合重新构建完整索引。
        # 规则：同 executor 内 name / alias 必须唯一，不同 executor内允许同名

        index: dict[tuple[str, str], ToolSpec] = {}
        for spec in specs:
            for key in self._keys_for(spec):
                existing = index.get(key)
                if (
                    existing is not None
                    and existing is not spec
                ):
                    executor, name = key
                    raise ValueError(
                        f"tool name/alias collision within executor {executor}: {name}"
                    )
                index[key] = spec
        return index


    def register(
        self,
        spec: ToolSpec,
        *,
        replace: bool = False,
    ) -> None:

        if not isinstance(
            spec,
            ToolSpec,
        ):
            raise TypeError("registry only accepts ToolSpec")

        canonical = self._normalize(spec.name)

        if not canonical:
            raise ValueError("tool name cannot be empty")

        replacement_target = None
        for existing in self._specs:
            if self._same_identity(
                existing,
                spec,
            ):
                replacement_target = existing
                break

        if (
            replacement_target
            is not None
            and not replace
        ):

            raise ValueError(
                "tool already registered: "
                f"{spec.name} "
                f"for executors "
                f"{sorted(spec.executors)}"
            )

        candidate_specs = [
            existing
            for existing
            in self._specs
            if existing
            is not replacement_target
        ]

        candidate_specs.append(
            spec
        )

        candidate_index = (
            self._build_index(
                candidate_specs
            )
        )


        self._specs = (
            candidate_specs
        )

        self._index = (
            candidate_index
        )

        self._version += 1

    def extend(
        self,
        specs: Iterable[
            ToolSpec
        ],
    ) -> None:

        for spec in specs:
            self.register(spec)

    def resolve(
        self,
        name: str,
        *,
        executor: str | None = None,
    ) -> ToolSpec | None:
        
        # 根据 name / alias 查找工具。

        normalized_name = self._normalize(name)

        if not normalized_name:
            return None

        # executor 已知。
        if executor is not None:
            normalized_executor = self._normalize_executor(executor)

            if not normalized_executor:
                return None

            return self._index.get(
                (
                    normalized_executor,
                    normalized_name,
                )
            )

        matches: list[ToolSpec] = []

        seen: set[int] = set()

        for (
            _indexed_executor,
            indexed_name,
        ), spec in (
            self._index.items()
        ):

            if (indexed_name!= normalized_name):
                continue

            identity = id(spec)

            if identity in seen:
                continue

            seen.add(identity)

            matches.append(spec)

        if not matches:
            return None

        if len(matches) == 1:
            return matches[0]

        raise AmbiguousToolError(
            f"tool name {name} "
            "exists in multiple executor "
            "namespaces; executor is required"
        )

    def require(
        self,
        name: str,
        *,
        executor: str | None = None,
    ) -> ToolSpec:

        spec = self.resolve(name,executor=executor)

        if spec is None:
            if executor is None:
                raise KeyError(f"unknown tool: {name}")

            raise KeyError(
                f"unknown tool: "
                f"{name} "
                f"for executor "
                f"{executor}"
            )

        return spec



    def unregister(
        self,
        name: str,
        *,
        executor: str | None = None,
    ) -> bool:
        """
        删除 ToolSpec
        可以使用：canonical name 或 alias
        如果名字跨 executor 有歧义，
        必须显式传 executor
        """

        spec = self.resolve(
            name,
            executor=executor,
        )

        if spec is None:
            return False

        candidate_specs = [
            existing
            for existing
            in self._specs
            if existing is not spec
        ]

        candidate_index = (
            self._build_index(
                candidate_specs
            )
        )

        self._specs = (
            candidate_specs
        )

        self._index = (
            candidate_index
        )

        self._version += 1

        return True


    def search(
        self,
        query: str,
        *,
        executor: str | None = None,
        limit: int = 8,
    ) -> list[ToolSpec]:

        query = str(
            query or ""
        ).strip().casefold()

        if not query:
            raise ValueError(
                "tool search query "
                "cannot be empty"
            )

        normalized_executor = None

        if executor is not None:
            normalized_executor = (
                self._normalize_executor(
                    executor
                )
            )

        effective_limit = max(
            1,
            int(limit),
        )

        terms = {
            term
            for term in query.split()
            if term
        }

        scored: list[
            tuple[
                int,
                str,
                ToolSpec,
            ]
        ] = []

        for spec in self._specs:

            if (
                normalized_executor
                is not None
                and normalized_executor
                not in {
                    self._normalize_executor(
                        executor_name
                    )
                    for executor_name
                    in spec.executors
                }
            ):
                continue

            names = tuple(
                self._normalize(
                    name
                )
                for name
                in spec.names
            )

            searchable = " ".join(
                (
                    *names,

                    spec.description
                    .casefold(),

                    spec.source
                    .casefold(),
                )
            )

            score = 0

            if query in names:
                score += 100

            elif any(
                query in name
                for name in names
            ):
                score += 70

            if query in searchable:
                score += 20

            score += (
                10
                * sum(
                    1
                    for term in terms
                    if term in searchable
                )
            )

            if score <= 0:
                continue

            scored.append(
                (
                    score,
                    spec.name.casefold(),
                    spec,
                )
            )

        scored.sort(
            key=lambda item: (
                -item[0],
                item[1],
            )
        )

        return [
            spec
            for (
                _score,
                _name,
                spec,
            )
            in scored[
                :effective_limit
            ]
        ]


def builtin_tool_specs(
) -> tuple[
    ToolSpec,
    ...
]:

    all_scopes = frozenset(
        {
            "session",
            "repository",
        }
    )

    return (
        ToolSpec(
            name="Read",
            description="读取文件或目录内容",
            aliases=(
                "read_file",
            ),
            executors=frozenset(
                {
                    "claude",
                }
            ),
            workdir_scopes=all_scopes,
            access=ToolAccess.READ_ONLY,
            side_effect=ToolSideEffect.NONE,
            idempotency=ToolIdempotency.SAFE,
            approval=ApprovalRequirement.NEVER,
            execution_mode=ToolExecutionMode.BACKEND_NATIVE,
            source="builtin",
        ),

        ToolSpec(
            name="Glob",
            description="按路径模式查找文件",
            executors=frozenset(
                {
                    "claude",
                }
            ),
            workdir_scopes=all_scopes,
            access=ToolAccess.READ_ONLY,
            side_effect=ToolSideEffect.NONE,
            idempotency=ToolIdempotency.SAFE,
            approval=ApprovalRequirement.NEVER,
            execution_mode=ToolExecutionMode.BACKEND_NATIVE,
            source="builtin",
        ),

        ToolSpec(
            name="Grep",
            description="在文件中搜索文本",
            aliases=(
                "search_text",
            ),
            executors=frozenset(
                {
                    "claude",
                }
            ),
            workdir_scopes=all_scopes,
            access=ToolAccess.READ_ONLY,
            side_effect=ToolSideEffect.NONE,
            idempotency=ToolIdempotency.SAFE,
            approval=ApprovalRequirement.NEVER,
            execution_mode=ToolExecutionMode.BACKEND_NATIVE,
            source="builtin",
        ),


        ToolSpec(
            name="Bash",
            description="执行 shell 命令；具体副作用取决于命令内容",
            aliases=(
                "shell",
            ),
            executors=frozenset(
                {
                    "claude",
                }
            ),
            workdir_scopes=all_scopes,
            access=ToolAccess.WRITE,
            side_effect=ToolSideEffect.UNKNOWN,
            idempotency=ToolIdempotency.UNKNOWN,
            approval=ApprovalRequirement.REQUIRED,
            execution_mode=ToolExecutionMode.BACKEND_NATIVE,
            source="builtin",
        ),

        ToolSpec(
            name="Edit",
            description=(
                "修改已有文件内容"
            ),
            aliases=(
                "edit_file",
            ),
            executors=frozenset(
                {
                    "claude",
                }
            ),
            workdir_scopes=(
                all_scopes
            ),
            access=(
                ToolAccess.WRITE
            ),
            side_effect=(
                ToolSideEffect.LOCAL_WRITE
            ),
            idempotency=(
                ToolIdempotency.UNKNOWN
            ),
            approval=(
                ApprovalRequirement.REQUIRED
            ),
            execution_mode=(
                ToolExecutionMode
                .BACKEND_NATIVE
            ),
            source="builtin",
        ),

        ToolSpec(
            name="Write",
            description=(
                "创建或覆盖文件"
            ),
            aliases=(
                "write_file",
            ),
            executors=frozenset(
                {
                    "claude",
                }
            ),
            workdir_scopes=(
                all_scopes
            ),
            access=(
                ToolAccess.WRITE
            ),
            side_effect=(
                ToolSideEffect.LOCAL_WRITE
            ),
            idempotency=(
                ToolIdempotency.UNKNOWN
            ),
            approval=(
                ApprovalRequirement.REQUIRED
            ),
            execution_mode=(
                ToolExecutionMode
                .BACKEND_NATIVE
            ),
            source="builtin",
        ),

        ToolSpec(
            name="WebSearch",
            description=(
                "搜索外部网页或资料"
            ),
            aliases=(
                "web_search",
            ),
            executors=frozenset(
                {
                    "claude",
                }
            ),
            workdir_scopes=(
                all_scopes
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
            approval=(
                ApprovalRequirement.NEVER
            ),
            execution_mode=(
                ToolExecutionMode
                .BACKEND_NATIVE
            ),
            source="builtin",
        ),

        ToolSpec(
            name="WebFetch",
            description=(
                "读取指定网页内容"
            ),
            aliases=(
                "web_fetch",
            ),
            executors=frozenset(
                {
                    "claude",
                }
            ),
            workdir_scopes=(
                all_scopes
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
            approval=(
                ApprovalRequirement.NEVER
            ),
            execution_mode=(
                ToolExecutionMode
                .BACKEND_NATIVE
            ),
            source="builtin",
        ),


        ToolSpec(
            name="Task",
            description=(
                "委派或协调子 Agent"
            ),
            aliases=(
                "subagent",
            ),
            executors=frozenset(
                {
                    "claude",
                }
            ),
            workdir_scopes=(
                all_scopes
            ),
            access=(
                ToolAccess.WRITE
            ),
            side_effect=(
                ToolSideEffect.UNKNOWN
            ),
            idempotency=(
                ToolIdempotency.UNKNOWN
            ),
            approval=(
                ApprovalRequirement.REQUIRED
            ),
            execution_mode=(
                ToolExecutionMode
                .BACKEND_NATIVE
            ),
            source="builtin",
        ),

        ToolSpec(
            name="NotebookRead",
            description=(
                "读取 notebook 内容"
            ),
            executors=frozenset(
                {
                    "claude",
                }
            ),
            workdir_scopes=(
                all_scopes
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
            approval=(
                ApprovalRequirement.NEVER
            ),
            execution_mode=(
                ToolExecutionMode
                .BACKEND_NATIVE
            ),
            source="builtin",
        ),


        ToolSpec(
            name="run_command",
            description=(
                "执行命令或程序；"
                "受 Codex sandbox "
                "和审批策略约束"
            ),
            aliases=(
                "command_execution",
            ),
            executors=frozenset(
                {
                    "codex",
                }
            ),
            workdir_scopes=(
                all_scopes
            ),
            access=(
                ToolAccess.WRITE
            ),
            side_effect=(
                ToolSideEffect.UNKNOWN
            ),
            idempotency=(
                ToolIdempotency.UNKNOWN
            ),
            approval=(
                ApprovalRequirement.REQUIRED
            ),
            execution_mode=(
                ToolExecutionMode
                .BACKEND_NATIVE
            ),
            source="builtin",
        ),

        ToolSpec(
            name="edit_file",
            description=(
                "编辑文件；受 Codex "
                "workspace policy 约束"
            ),
            executors=frozenset(
                {
                    "codex",
                }
            ),
            workdir_scopes=(
                all_scopes
            ),
            access=(
                ToolAccess.WRITE
            ),
            side_effect=(
                ToolSideEffect.LOCAL_WRITE
            ),
            idempotency=(
                ToolIdempotency.UNKNOWN
            ),
            approval=(
                ApprovalRequirement.REQUIRED
            ),
            execution_mode=(
                ToolExecutionMode
                .BACKEND_NATIVE
            ),
            source="builtin",
        ),

        ToolSpec(
            name="web_search",
            description=(
                "搜索外部网页或资料"
            ),
            executors=frozenset(
                {
                    "codex",
                }
            ),
            workdir_scopes=(
                all_scopes
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
            approval=(
                ApprovalRequirement.NEVER
            ),
            execution_mode=(
                ToolExecutionMode
                .BACKEND_NATIVE
            ),
            source="builtin",
        ),

        ToolSpec(
            name="mcp_tool_call",
            description=(
                "调用已连接的 MCP 工具；"
                "具体副作用取决于 MCP Tool"
            ),
            aliases=(
                "mcp",
            ),
            executors=frozenset(
                {
                    "codex",
                }
            ),
            workdir_scopes=(
                all_scopes
            ),
            access=(
                ToolAccess.WRITE
            ),
            side_effect=(
                ToolSideEffect.UNKNOWN
            ),
            idempotency=(
                ToolIdempotency.UNKNOWN
            ),
            approval=(
                ApprovalRequirement.REQUIRED
            ),
            execution_mode=(
                ToolExecutionMode
                .MCP
            ),
            source="builtin",
        ),

        ToolSpec(
            name="memory_read",
            description=(
                "读取 Agent Memory "
                "或运行时上下文"
            ),
            executors=frozenset(
                {
                    "codex",
                }
            ),
            workdir_scopes=(
                all_scopes
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
            approval=(
                ApprovalRequirement.NEVER
            ),
            execution_mode=(
                ToolExecutionMode
                .BACKEND_NATIVE
            ),
            source="builtin",
        ),

        ToolSpec(
            name="memory_write",
            description=(
                "写入 Agent Memory "
                "或运行时上下文"
            ),
            executors=frozenset(
                {
                    "codex",
                }
            ),
            workdir_scopes=(
                all_scopes
            ),
            access=(
                ToolAccess.WRITE
            ),
            side_effect=(
                ToolSideEffect.LOCAL_WRITE
            ),
            idempotency=(
                ToolIdempotency.UNKNOWN
            ),
            approval=(
                ApprovalRequirement.REQUIRED
            ),
            execution_mode=(
                ToolExecutionMode
                .BACKEND_NATIVE
            ),
            source="builtin",
        ),
    )


def default_tool_registry(
) -> ToolRegistry:

    return ToolRegistry(
        builtin_tool_specs()
    )