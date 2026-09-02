"""工具调用前后置策略钩子。

钩子是可组合的策略层，不负责执行工具。匹配条件只支持精确名称、字符串
包含和路径前缀，避免重新引入正则表达式，同时让危险操作的阻断原因可审计。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


def _strings(value: Any, *, depth: int = 0) -> Iterable[str]:
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item, depth=depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _strings(item, depth=depth + 1)


def _normalize_path(value: str) -> str:
    return str(value or "").replace("\\", "/").casefold().rstrip("/")


def _path_has_prefix(path: str, prefix: str) -> bool:
    normalized_path = _normalize_path(path)
    normalized_prefix = _normalize_path(prefix)
    return normalized_path == normalized_prefix or normalized_path.startswith(normalized_prefix + "/")


@dataclass(frozen=True)
class HookContext:
    executor: str
    task_id: str
    attempt_id: str
    tool_name: str
    input_data: dict
    workdir: str


@dataclass(frozen=True)
class HookOutcome:
    allowed: bool = True
    warnings: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        return "；".join((*self.blocked_by, *self.warnings))


@dataclass(frozen=True)
class HookRule:
    name: str
    phase: str = "before_tool"
    action: str = "warn"
    enabled: bool = True
    tool_names: tuple[str, ...] = ()
    contains: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()
    message: str = ""

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "HookRule":
        if not isinstance(value, dict):
            raise ValueError("hooks.rules 中的规则必须是对象")

        def strings(name: str) -> tuple[str, ...]:
            raw = value.get(name, ())
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, (list, tuple, set)) or not all(isinstance(item, str) for item in raw):
                raise ValueError(f"hooks.rules.{name} 必须是字符串数组")
            return tuple(item.strip() for item in raw if item.strip())

        phase = str(value.get("phase", "before_tool") or "before_tool").strip().lower()
        action = str(value.get("action", "warn") or "warn").strip().lower()
        if phase not in {"before_tool", "after_tool", "all"}:
            raise ValueError(f"hook phase 不支持: {phase}")
        if action not in {"warn", "block"}:
            raise ValueError(f"hook action 不支持: {action}")
        return cls(
            name=str(value.get("name", "unnamed-hook") or "unnamed-hook"),
            phase=phase,
            action=action,
            enabled=bool(value.get("enabled", True)),
            tool_names=strings("tool_names"),
            contains=strings("contains"),
            path_prefixes=strings("path_prefixes"),
            message=str(value.get("message", "") or ""),
        )

    def matches(self, context: HookContext) -> bool:
        if not self.enabled:
            return False
        if self.tool_names:
            names = {item.casefold() for item in self.tool_names}
            actual = context.tool_name.casefold()
            if "*" not in names and actual not in names:
                return False
        values = tuple(_strings(context.input_data))
        if self.contains:
            lowered = tuple(item.casefold() for item in values)
            if not all(any(pattern.casefold() in value for value in lowered) for pattern in self.contains):
                return False
        if self.path_prefixes:
            paths: list[str] = []
            for key in ("path", "file_path", "cwd", "workdir", "target"):
                value = context.input_data.get(key)
                paths.extend(_normalize_path(item) for item in _strings(value))
            if not any(
                _path_has_prefix(path, prefix)
                for path in paths
                for prefix in self.path_prefixes
            ):
                return False
        return bool(self.tool_names or self.contains or self.path_prefixes)


class HookChain:
    """按配置顺序执行钩子，阻断优先于警告。"""

    def __init__(self, rules: Iterable[HookRule] = ()) -> None:
        self.rules = tuple(rules)

    @classmethod
    def from_config(cls, cfg) -> "HookChain":
        hook_cfg = getattr(cfg, "hooks", None)
        raw_rules = getattr(hook_cfg, "rules", ()) if hook_cfg is not None else ()
        return cls(HookRule.from_mapping(item) for item in (raw_rules or ()))

    def before_tool(self, context: HookContext) -> HookOutcome:
        return self._evaluate(context, "before_tool", allow_block=True)

    def after_tool(self, context: HookContext, result: Any = None) -> HookOutcome:
        del result
        return self._evaluate(context, "after_tool", allow_block=False)

    def _evaluate(self, context: HookContext, phase: str, *, allow_block: bool) -> HookOutcome:
        warnings: list[str] = []
        blocked: list[str] = []
        for rule in self.rules:
            if rule.phase not in {phase, "all"} or not rule.matches(context):
                continue
            message = rule.message or f"hook {rule.name} 匹配工具 {context.tool_name}"
            message = message[:320]
            if allow_block and rule.action == "block":
                blocked.append(f"[{rule.name}] {message}")
            else:
                warnings.append(f"[{rule.name}] {message}")
        return HookOutcome(allowed=not blocked, warnings=tuple(warnings), blocked_by=tuple(blocked))
