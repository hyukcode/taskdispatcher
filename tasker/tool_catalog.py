"""工具元数据目录、延迟搜索和执行能力校验。

工具目录只负责回答两个问题：当前执行器有哪些工具，以及某个任务是否可以
使用它们。真正的工具执行仍由 Claude SDK/Codex App Server 负责，避免 tasker
为了“发现工具”而绕过后端的沙箱和审批边界。

实现故意使用字符串、集合和简单评分，不依赖正则表达式。这样目录既能处理
中文提示，也能保持和项目其它 ID/路径校验逻辑一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


_EXECUTORS = frozenset(("claude", "codex"))
_WORKDIR_SCOPES = frozenset(("session", "repository"))
_ACCESS_LEVELS = frozenset(("read_only", "write"))


def _is_term_character(character: str) -> bool:
    """判断一个字符是否属于工具搜索中的词片段。"""
    return character.isalnum() or character == "_"


def _tokens(value: str) -> tuple[str, ...]:
    """把中英文工具名/描述切成稳定的词片段，不使用正则。"""
    result: list[str] = []
    current: list[str] = []
    for character in str(value or ""):
        if _is_term_character(character):
            current.append(character.casefold())
            continue
        if current:
            result.append("".join(current))
            current = []
    if current:
        result.append("".join(current))
    return tuple(result)


def _normalized_names(values: Iterable[str]) -> frozenset[str]:
    return frozenset(str(value).strip().casefold() for value in values if str(value).strip())


@dataclass(frozen=True)
class ToolDescriptor:
    """工具的可搜索元数据，而不是工具的执行句柄。"""

    name: str
    description: str
    executors: frozenset[str] = _EXECUTORS
    aliases: tuple[str, ...] = ()
    access: str = "read_only"
    workdir_scopes: frozenset[str] = _WORKDIR_SCOPES
    requires_approval: bool = False
    source: str = "builtin"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("工具名称不能为空")
        if not self.executors or not self.executors.issubset(_EXECUTORS):
            raise ValueError(f"工具 {self.name} 的 executors 非法: {self.executors}")
        if self.access not in _ACCESS_LEVELS:
            raise ValueError(f"工具 {self.name} 的 access 非法: {self.access}")
        if not self.workdir_scopes or not self.workdir_scopes.issubset(_WORKDIR_SCOPES):
            raise ValueError(f"工具 {self.name} 的 workdir_scopes 非法: {self.workdir_scopes}")

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, source: str = "config") -> "ToolDescriptor":
        if not isinstance(value, dict):
            raise ValueError("工具目录项必须是 JSON 对象")
        raw_executors = value.get("executors", tuple(_EXECUTORS))
        raw_scopes = value.get("workdir_scopes", tuple(_WORKDIR_SCOPES))
        raw_aliases = value.get("aliases", ())
        if isinstance(raw_executors, str):
            raw_executors = [raw_executors]
        if isinstance(raw_scopes, str):
            raw_scopes = [raw_scopes]
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        if not isinstance(raw_executors, (list, tuple, set)):
            raise ValueError("工具 executors 必须是字符串数组")
        if not isinstance(raw_scopes, (list, tuple, set)):
            raise ValueError("工具 workdir_scopes 必须是字符串数组")
        if not isinstance(raw_aliases, (list, tuple, set)):
            raise ValueError("工具 aliases 必须是字符串数组")
        return cls(
            name=str(value.get("name", "")),
            description=str(value.get("description", "") or ""),
            executors=frozenset(str(item) for item in raw_executors),
            aliases=tuple(str(item) for item in raw_aliases),
            access=str(value.get("access", "read_only") or "read_only"),
            workdir_scopes=frozenset(str(item) for item in raw_scopes),
            requires_approval=bool(value.get("requires_approval", False)),
            source=str(value.get("source", source) or source),
        )


@dataclass(frozen=True)
class ToolDecision:
    allowed: bool
    reason: str
    descriptor: ToolDescriptor | None = None


def _builtin_descriptors() -> tuple[ToolDescriptor, ...]:
    """返回两种后端都会用到的常见工具元数据。"""
    read = frozenset(("read_only", "write"))
    write = frozenset(("write",))
    all_scopes = frozenset(("session", "repository"))
    return (
        ToolDescriptor("Read", "读取文件或目录内容", frozenset(("claude",)), ("read_file",), "read_only", all_scopes),
        ToolDescriptor("Glob", "按路径模式查找文件", frozenset(("claude",)), (), "read_only", all_scopes),
        ToolDescriptor("Grep", "在文件中搜索文本", frozenset(("claude",)), ("search_text",), "read_only", all_scopes),
        ToolDescriptor("Bash", "执行 shell 命令；可能修改文件或产生外部副作用", frozenset(("claude",)), ("shell",), "write", all_scopes, True),
        ToolDescriptor("Edit", "修改已有文件内容", frozenset(("claude",)), ("edit_file",), "write", all_scopes, True),
        ToolDescriptor("Write", "创建或覆盖文件", frozenset(("claude",)), ("write_file",), "write", all_scopes, True),
        ToolDescriptor("WebSearch", "搜索外部网页或资料", frozenset(("claude",)), ("web_search",), "read_only", all_scopes),
        ToolDescriptor("WebFetch", "读取指定网页内容", frozenset(("claude",)), ("web_fetch",), "read_only", all_scopes),
        ToolDescriptor("Task", "委派或协调子 Agent", frozenset(("claude",)), ("subagent",), "write", all_scopes, True),
        ToolDescriptor("NotebookRead", "读取 notebook 内容", frozenset(("claude",)), (), "read_only", all_scopes),
        ToolDescriptor("run_command", "执行命令或程序；受 Codex sandbox 和审批策略约束", frozenset(("codex",)), ("command_execution",), "write", all_scopes, True),
        ToolDescriptor("edit_file", "编辑文件；受 Codex workspace policy 约束", frozenset(("codex",)), ("file_change",), "write", all_scopes, True),
        ToolDescriptor("web_search", "搜索外部网页或资料", frozenset(("codex",)), (), "read_only", all_scopes),
        ToolDescriptor("mcp_tool_call", "调用已连接且通过策略检查的 MCP 工具", frozenset(("codex",)), ("mcp",), "write", all_scopes, True),
        ToolDescriptor("memory_read", "读取 Agent 记忆或上下文", frozenset(("codex",)), (), "read_only", all_scopes),
        ToolDescriptor("memory_write", "写入 Agent 记忆或上下文", frozenset(("codex",)), (), "write", all_scopes, True),
    )


class ToolCatalog:
    """支持延迟搜索、缓存和显式权限过滤的工具目录。"""

    def __init__(
        self,
        descriptors: Iterable[ToolDescriptor] = (),
        *,
        allowed_by_executor: dict[str, Iterable[str]] | None = None,
        denied_by_executor: dict[str, Iterable[str]] | None = None,
        search_limit: int = 8,
        max_description_chars: int = 320,
    ) -> None:
        self._descriptors: dict[str, ToolDescriptor] = {}
        self._allowed = {
            executor: _normalized_names(values)
            for executor, values in (allowed_by_executor or {}).items()
        }
        self._denied = {
            executor: _normalized_names(values)
            for executor, values in (denied_by_executor or {}).items()
        }
        self.search_limit = max(1, int(search_limit))
        self.max_description_chars = max(80, int(max_description_chars))
        self._version = 0
        self._cache: dict[tuple[Any, ...], tuple[ToolDescriptor, ...]] = {}
        initial = tuple(descriptors)
        self.extend(initial or _builtin_descriptors())

    @classmethod
    def from_config(cls, cfg) -> "ToolCatalog":
        """从 tasker 配置构造目录；未知的 Claude allow 工具也会进入目录。"""
        claude = getattr(cfg, "claude", None)
        catalog_cfg = getattr(cfg, "tools", None)
        allowed = {"claude": getattr(claude, "allowed_tools", ())}
        denied = {"claude": getattr(claude, "disallowed_tools", ())}
        descriptors = list(_builtin_descriptors())
        configured = getattr(catalog_cfg, "entries", ()) if catalog_cfg is not None else ()
        for item in configured or ():
            descriptors.append(ToolDescriptor.from_mapping(item))

        known = {name.casefold() for descriptor in descriptors for name in descriptor.names}
        for name in allowed["claude"]:
            normalized = str(name).strip()
            if normalized and normalized.casefold() not in known and normalized != "*":
                descriptors.append(
                    ToolDescriptor(
                        name=normalized,
                        description=f"Claude 配置允许的工具：{normalized}",
                        executors=frozenset(("claude",)),
                        access="write",
                        requires_approval=True,
                        source="claude-config",
                    )
                )
                known.add(normalized.casefold())

        return cls(
            descriptors,
            allowed_by_executor=allowed,
            denied_by_executor=denied,
            search_limit=getattr(catalog_cfg, "search_limit", 8) if catalog_cfg else 8,
            max_description_chars=getattr(catalog_cfg, "max_description_chars", 320) if catalog_cfg else 320,
        )

    @property
    def version(self) -> int:
        return self._version

    @property
    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return tuple(self._descriptors.values())

    def register(self, descriptor: ToolDescriptor) -> None:
        if not isinstance(descriptor, ToolDescriptor):
            raise TypeError("工具目录只能注册 ToolDescriptor")
        self._descriptors[descriptor.name.casefold()] = descriptor
        self.invalidate()

    def extend(self, descriptors: Iterable[ToolDescriptor]) -> None:
        for descriptor in descriptors:
            self._descriptors[descriptor.name.casefold()] = descriptor
        self.invalidate()

    def invalidate(self) -> None:
        self._version += 1
        self._cache.clear()

    def _configured(self, descriptor: ToolDescriptor, executor: str) -> bool:
        names = {name.casefold() for name in descriptor.names}
        denied = self._denied.get(executor, frozenset())
        if "*" in denied or names.intersection(denied):
            return False
        allowed = self._allowed.get(executor, frozenset())
        if allowed and "*" not in allowed and not names.intersection(allowed):
            return False
        return True

    def decision(
        self,
        name: str,
        *,
        executor: str,
        workspace_access: str = "write",
        workdir_scope: str = "session",
    ) -> ToolDecision:
        candidate = str(name or "").strip().casefold()
        if not candidate:
            return ToolDecision(False, "工具名称不能为空")
        for descriptor in self._descriptors.values():
            names = {item.casefold() for item in descriptor.names}
            if candidate not in names:
                continue
            if executor not in descriptor.executors:
                return ToolDecision(False, f"工具 {name} 不支持 executor {executor}", descriptor)
            if workspace_access == "read_only" and descriptor.access == "write":
                return ToolDecision(False, f"只读任务不能使用写工具 {name}", descriptor)
            if workdir_scope not in descriptor.workdir_scopes:
                return ToolDecision(False, f"工具 {name} 不支持工作目录范围 {workdir_scope}", descriptor)
            if not self._configured(descriptor, executor):
                return ToolDecision(False, f"工具 {name} 未通过 {executor} 的 allow/deny 配置", descriptor)
            return ToolDecision(True, "工具已通过能力与策略校验", descriptor)
        return ToolDecision(False, f"工具 {name} 未注册")

    def load(
        self,
        name: str,
        *,
        executor: str,
        workspace_access: str = "write",
        workdir_scope: str = "session",
    ) -> ToolDescriptor:
        result = self.decision(
            name,
            executor=executor,
            workspace_access=workspace_access,
            workdir_scope=workdir_scope,
        )
        if not result.allowed or result.descriptor is None:
            raise PermissionError(result.reason)
        return result.descriptor

    @staticmethod
    def _score(descriptor: ToolDescriptor, query: str) -> int:
        query_value = query.strip().casefold()
        if not query_value:
            return 1
        names = [item.casefold() for item in descriptor.names]
        searchable = " ".join((*names, descriptor.description.casefold()))
        score = 0
        if query_value in names:
            score += 100
        elif any(query_value in name for name in names):
            score += 70
        if query_value in searchable:
            score += 20
        query_terms = set(_tokens(query_value))
        searchable_terms = set(_tokens(searchable))
        score += 10 * len(query_terms.intersection(searchable_terms))
        return score

    def search(
        self,
        query: str,
        *,
        executor: str,
        workspace_access: str = "write",
        workdir_scope: str = "session",
        limit: int | None = None,
    ) -> list[ToolDescriptor]:
        """按需搜索当前策略允许的工具，并缓存同一目录版本的结果。"""
        query = str(query or "").strip()
        if not query:
            raise ValueError("工具搜索 query 不能为空")
        effective_limit = self.search_limit if limit is None else max(1, int(limit))
        key = (self._version, query.casefold(), executor, workspace_access, workdir_scope, effective_limit)
        cached = self._cache.get(key)
        if cached is not None:
            return list(cached)

        scored: list[tuple[int, ToolDescriptor]] = []
        for descriptor in self._descriptors.values():
            if executor not in descriptor.executors:
                continue
            if workspace_access == "read_only" and descriptor.access == "write":
                continue
            if workdir_scope not in descriptor.workdir_scopes:
                continue
            if not self._configured(descriptor, executor):
                continue
            score = self._score(descriptor, query)
            if score > 0:
                scored.append((score, descriptor))
        scored.sort(key=lambda item: (-item[0], item[1].name.casefold()))
        result = tuple(item[1] for item in scored[:effective_limit])
        self._cache[key] = result
        return list(result)

    def describe_for(
        self,
        query: str,
        *,
        executor: str,
        workspace_access: str,
        workdir_scope: str,
        limit: int | None = None,
    ) -> str:
        """生成给 Agent 的有界工具目录片段。"""
        descriptors = self.search(
            query or "工具",
            executor=executor,
            workspace_access=workspace_access,
            workdir_scope=workdir_scope,
            limit=limit,
        )
        if not descriptors:
            return "（当前策略下没有匹配的已注册工具；请使用后端实际提供的等价能力。）"
        lines = []
        for descriptor in descriptors:
            description = descriptor.description[: self.max_description_chars]
            approval = "需审批" if descriptor.requires_approval else "无需额外审批提示"
            lines.append(f"- {descriptor.name}: {description}（{descriptor.access}, {approval}, 来源={descriptor.source}）")
        return "\n".join(lines)

    def prompt_catalog(self, *, max_tools: int = 32) -> str:
        """返回 planner 使用的全局能力概览，不注入任何执行句柄。"""
        lines = ["## 可发现工具目录（仅元数据，实际调用仍受后端策略和审批约束）"]
        visible = []
        for descriptor in self.descriptors:
            executors = tuple(
                executor for executor in sorted(descriptor.executors)
                if self._configured(descriptor, executor)
            )
            if executors:
                visible.append((descriptor, executors))
        for descriptor, executors in sorted(visible, key=lambda item: item[0].name.casefold())[:max_tools]:
            description = descriptor.description[: self.max_description_chars]
            executor_text = "/".join(executors)
            lines.append(f"- {descriptor.name} [{executor_text}, {descriptor.access}]: {description}")
        return "\n".join(lines)
