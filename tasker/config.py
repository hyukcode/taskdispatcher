from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _is_env_name(value: object) -> bool:
    """按 POSIX/常见 shell 规则校验环境变量名，不依赖正则表达式。"""
    if not isinstance(value, str) or not value:
        return False
    first = value[0]
    if not (first == "_" or (first.isascii() and first.isalpha())):
        return False
    return all(
        character == "_" or (character.isascii() and (character.isalpha() or character.isdigit()))
        for character in value[1:]
    )


@dataclass
class LLMConfig:

    provider: str = "anthropic"
    base_url: str = ""
    api_key_env: str = "ANTHROPIC_API_KEY"
    model: str = "claude-sonnet-5"
    temperature: float = 0.0
    max_tokens: int = 8000
    timeout: float = 120.0


@dataclass
class ClaudeConfig:
    model: str = ""
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    completion_idle: float = 5.0


@dataclass
class CodexConfig:
    binary: str = "codex"
    model: str = ""
    # read-only | workspace-write | danger-full-access
    sandbox: str = "workspace-write"
    extra_args: list[str] = field(default_factory=list)
    approval_policy: str = "on-request"
    completion_idle: float = 5.0


@dataclass
class ApprovalConfig:
    """审批请求处理方式。"""

    mode: str = "auto"  # auto | log | ask_console
    default_allow: bool = True
    timeout: float = 120.0


@dataclass
class DispatchConfig:
    """任务分派策略。"""

    strategy: str = "codex-first-review"
    complex_executor: str = "codex"
    implementation_executor: str = "claude"
    verification_executor: str = "codex"
    claude_model: str = "DeepSeek-v4-pro"
    codex_model: str = "ChatGPT-5.6"
    # 当前代码 agent 失败时，是否允许切换到另一种代码 agent 重试。
    failover_enabled: bool = True
    max_failover_attempts: int = 1


@dataclass
class RetryConfig:
    """同一 executor 的临时失败重试策略。"""

    max_retries: int = 1
    initial_delay: float = 1.0
    max_delay: float = 30.0


@dataclass
class ReviewConfig:
    """实现后的独立审查与证据验证策略。"""

    enabled: bool = False
    reviewer_count: int = 2
    min_confidence: int = 80
    require_evidence: bool = True


@dataclass
class HookConfig:
    """工具调用前后置策略；规则只使用字符串和路径判断。"""

    rules: list[dict] = field(default_factory=list)


@dataclass
class RuntimeConfig:
    """运行时队列和上下文预算，防止长任务无限膨胀。"""

    input_queue_maxsize: int = 64
    event_queue_maxsize: int = 2048
    max_context_chars: int = 16000
    max_dependency_chars: int = 8000
    max_state_chars: int = 2000


@dataclass
class GoalLoopConfig:
    """外层收敛循环配置。"""

    max_iterations: int = 1 
    evaluator: str = "codex"

@dataclass
class TemplateCompilerConfig:

    loop_infer: str = "llm"
    cache: bool = True


@dataclass
class ToolCatalogConfig:
    """工具元数据目录配置；不包含可执行代码或命令。"""

    search_limit: int = 8
    max_description_chars: int = 320
    entries: list[dict] = field(default_factory=list)


@dataclass
class DisplayConfig:

    level: str = "minimal"  # minimal | verbose | debug


@dataclass
class SessionConfig:

    dir: str = "~/.tasker/sessions"
    workspace_dir: str = "~/.tasker/workspace"

    @property
    def path(self) -> Path:
        return Path(self.dir).expanduser()

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace_dir).expanduser()


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    hooks: HookConfig = field(default_factory=HookConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    goal_loop: GoalLoopConfig = field(default_factory=GoalLoopConfig)
    template_compiler: TemplateCompilerConfig = field(default_factory=TemplateCompilerConfig)
    tools: ToolCatalogConfig = field(default_factory=ToolCatalogConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    max_parallel: int = 2
    timeout_per_task: float = 900.0

    def validate(self) -> "Config":
        """校验配置并在加载边界尽早报告错误。"""
        if not isinstance(self.llm.provider, str) or self.llm.provider not in {"anthropic", "openai"}:
            raise ValueError(f"llm.provider 不支持: {self.llm.provider}")
        if not _is_env_name(self.llm.api_key_env):
            raise ValueError(f"llm.api_key_env 不是合法环境变量名: {self.llm.api_key_env}")
        if not 0 <= self.llm.temperature <= 2:
            raise ValueError("llm.temperature 必须在 0 到 2 之间")
        if self.llm.max_tokens <= 0 or self.llm.timeout <= 0:
            raise ValueError("llm.max_tokens 和 llm.timeout 必须大于 0")

        if self.claude.permission_mode not in {
            "default", "acceptEdits", "bypassPermissions", "plan", "dontAsk",
        }:
            raise ValueError(f"claude.permission_mode 不支持: {self.claude.permission_mode}")
        for name, values in (
            ("claude.allowed_tools", self.claude.allowed_tools),
            ("claude.disallowed_tools", self.claude.disallowed_tools),
            ("codex.extra_args", self.codex.extra_args),
        ):
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError(f"{name} 必须是字符串数组")

        if self.codex.sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError(f"codex.sandbox 不支持: {self.codex.sandbox}")
        if self.codex.approval_policy not in {
            "on-request", "onRequest", "on-failure", "onFailure",
            "untrusted", "unless-trusted", "unlessTrusted", "granular", "never",
        }:
            raise ValueError(f"codex.approval_policy 不支持: {self.codex.approval_policy}")

        if self.approval.mode not in {"auto", "log", "ask_console"}:
            raise ValueError(f"approval.mode 不支持: {self.approval.mode}")
        if self.approval.timeout <= 0:
            raise ValueError("approval.timeout 必须大于 0")
        if (
            not isinstance(self.retry.max_retries, int)
            or isinstance(self.retry.max_retries, bool)
            or self.retry.max_retries < 0
        ):
            raise ValueError("retry.max_retries 必须是大于等于 0 的整数")
        if self.retry.initial_delay <= 0 or self.retry.max_delay <= 0:
            raise ValueError("retry.initial_delay 和 retry.max_delay 必须大于 0")
        if self.retry.initial_delay > self.retry.max_delay:
            raise ValueError("retry.initial_delay 不能大于 retry.max_delay")
        if (
            not isinstance(self.review.reviewer_count, int)
            or isinstance(self.review.reviewer_count, bool)
            or self.review.reviewer_count < 0
        ):
            raise ValueError("review.reviewer_count 必须是大于等于 0 的整数")
        if not 0 <= self.review.min_confidence <= 100:
            raise ValueError("review.min_confidence 必须在 0 到 100 之间")
        if not isinstance(self.review.enabled, bool):
            raise ValueError("review.enabled 必须是布尔值")
        if not isinstance(self.review.require_evidence, bool):
            raise ValueError("review.require_evidence 必须是布尔值")
        if not isinstance(self.hooks.rules, list) or not all(isinstance(item, dict) for item in self.hooks.rules):
            raise ValueError("hooks.rules 必须是对象数组")
        try:
            from .policy_hooks import HookRule

            for item in self.hooks.rules:
                HookRule.from_mapping(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"hooks.rules 配置非法: {exc}") from exc
        for name, value in (
            ("runtime.input_queue_maxsize", self.runtime.input_queue_maxsize),
            ("runtime.event_queue_maxsize", self.runtime.event_queue_maxsize),
            ("runtime.max_context_chars", self.runtime.max_context_chars),
            ("runtime.max_dependency_chars", self.runtime.max_dependency_chars),
            ("runtime.max_state_chars", self.runtime.max_state_chars),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} 必须是大于 0 的整数")
        if self.goal_loop.max_iterations <= 0:
            raise ValueError("goal_loop.max_iterations 必须大于 0")
        if self.goal_loop.evaluator not in {"claude", "codex"}:
            raise ValueError(f"goal_loop.evaluator 必须是 claude 或 codex: {self.goal_loop.evaluator}")
        if self.template_compiler.loop_infer not in {"llm", "off"}:
            raise ValueError(f"template_compiler.loop_infer 不支持: {self.template_compiler.loop_infer}")
        if self.tools.search_limit <= 0 or self.tools.max_description_chars <= 0:
            raise ValueError("tools.search_limit 和 tools.max_description_chars 必须大于 0")
        if not isinstance(self.tools.entries, list) or not all(isinstance(item, dict) for item in self.tools.entries):
            raise ValueError("tools.entries 必须是对象数组")
        try:
            from .tool_catalog import ToolDescriptor

            for item in self.tools.entries:
                ToolDescriptor.from_mapping(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"tools.entries 配置非法: {exc}") from exc
        if self.display.level not in {"minimal", "verbose", "debug"}:
            raise ValueError(f"display.level 不支持: {self.display.level}")
        if self.max_parallel <= 0:
            raise ValueError("max_parallel 必须大于 0")
        if self.timeout_per_task <= 0:
            raise ValueError("timeout_per_task 必须大于 0")
        if not self.session.dir or not self.session.workspace_dir:
            raise ValueError("session.dir 和 session.workspace_dir 不能为空")
        for name, value in (
            ("dispatch.complex_executor", self.dispatch.complex_executor),
            ("dispatch.implementation_executor", self.dispatch.implementation_executor),
            ("dispatch.verification_executor", self.dispatch.verification_executor),
        ):
            if value not in {"claude", "codex", "human"}:
                raise ValueError(f"{name} 不支持: {value}")
        if not isinstance(self.dispatch.failover_enabled, bool):
            raise ValueError("dispatch.failover_enabled 必须是布尔值")
        if (
            not isinstance(self.dispatch.max_failover_attempts, int)
            or isinstance(self.dispatch.max_failover_attempts, bool)
            or self.dispatch.max_failover_attempts < 0
        ):
            raise ValueError("dispatch.max_failover_attempts 必须是大于等于 0 的整数")
        return self


def _merge_cfg(cfg: Config, data: dict[str, Any]) -> Config:
    if not isinstance(data, dict):
        raise ValueError("配置根节点必须是 JSON 对象")

    def section(name: str) -> dict[str, Any] | None:
        value = data.get(name)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"配置段 {name} 必须是 JSON 对象")
        return value

    def as_bool(value: Any, name: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        raise ValueError(f"{name} 必须是布尔值")

    llm = cfg.llm
    d = section("llm")
    if d is not None:
        llm.provider = d.get("provider", llm.provider)
        llm.base_url = d.get("base_url", llm.base_url)
        llm.api_key_env = d.get("api_key_env", llm.api_key_env)
        llm.model = d.get("model", llm.model)
        llm.temperature = float(d.get("temperature", llm.temperature))
        llm.max_tokens = int(d.get("max_tokens", llm.max_tokens))
        llm.timeout = float(d.get("timeout", llm.timeout))

    d = section("claude")
    if d is not None:
        cfg.claude.model = d.get("model", cfg.claude.model)
        cfg.claude.permission_mode = d.get("permission_mode", cfg.claude.permission_mode)
        cfg.claude.allowed_tools = d.get("allowed_tools", cfg.claude.allowed_tools)
        cfg.claude.disallowed_tools = d.get("disallowed_tools", cfg.claude.disallowed_tools)
        cfg.claude.completion_idle = float(d.get("completion_idle", cfg.claude.completion_idle))

    d = section("codex")
    if d is not None:
        cfg.codex.binary = d.get("binary", cfg.codex.binary)
        cfg.codex.model = d.get("model", cfg.codex.model)
        cfg.codex.sandbox = d.get("sandbox", cfg.codex.sandbox)
        cfg.codex.extra_args = d.get("extra_args", cfg.codex.extra_args)
        cfg.codex.approval_policy = d.get("approval_policy", cfg.codex.approval_policy)
        cfg.codex.completion_idle = float(d.get("completion_idle", cfg.codex.completion_idle))

    d = section("approval")
    if d is not None:
        cfg.approval.mode = d.get("mode", cfg.approval.mode)
        if "default_allow" in d:
            cfg.approval.default_allow = as_bool(d["default_allow"], "approval.default_allow")
        cfg.approval.timeout = float(d.get("timeout", cfg.approval.timeout))

    d = section("retry")
    if d is not None:
        cfg.retry.max_retries = int(d.get("max_retries", cfg.retry.max_retries))
        cfg.retry.initial_delay = float(d.get("initial_delay", cfg.retry.initial_delay))
        cfg.retry.max_delay = float(d.get("max_delay", cfg.retry.max_delay))

    d = section("review")
    if d is not None:
        if "enabled" in d:
            cfg.review.enabled = as_bool(d["enabled"], "review.enabled")
        cfg.review.reviewer_count = int(d.get("reviewer_count", cfg.review.reviewer_count))
        cfg.review.min_confidence = int(d.get("min_confidence", cfg.review.min_confidence))
        if "require_evidence" in d:
            cfg.review.require_evidence = as_bool(d["require_evidence"], "review.require_evidence")

    d = section("hooks")
    if d is not None:
        cfg.hooks.rules = d.get("rules", cfg.hooks.rules)

    d = section("runtime")
    if d is not None:
        cfg.runtime.input_queue_maxsize = int(d.get("input_queue_maxsize", cfg.runtime.input_queue_maxsize))
        cfg.runtime.event_queue_maxsize = int(d.get("event_queue_maxsize", cfg.runtime.event_queue_maxsize))
        cfg.runtime.max_context_chars = int(d.get("max_context_chars", cfg.runtime.max_context_chars))
        cfg.runtime.max_dependency_chars = int(d.get("max_dependency_chars", cfg.runtime.max_dependency_chars))
        cfg.runtime.max_state_chars = int(d.get("max_state_chars", cfg.runtime.max_state_chars))

    d = section("dispatch")
    if d is not None:
        cfg.dispatch.strategy = d.get("strategy", cfg.dispatch.strategy)
        cfg.dispatch.complex_executor = d.get("complex_executor", cfg.dispatch.complex_executor)
        cfg.dispatch.implementation_executor = d.get("implementation_executor", cfg.dispatch.implementation_executor)
        cfg.dispatch.verification_executor = d.get("verification_executor", cfg.dispatch.verification_executor)
        cfg.dispatch.claude_model = d.get("claude_model", cfg.dispatch.claude_model)
        cfg.dispatch.codex_model = d.get("codex_model", cfg.dispatch.codex_model)
        if "failover_enabled" in d:
            cfg.dispatch.failover_enabled = as_bool(d["failover_enabled"], "dispatch.failover_enabled")
        cfg.dispatch.max_failover_attempts = int(
            d.get("max_failover_attempts", cfg.dispatch.max_failover_attempts)
        )

    d = section("goal_loop")
    if d is not None:
        cfg.goal_loop.max_iterations = int(d.get("max_iterations", cfg.goal_loop.max_iterations))
        cfg.goal_loop.evaluator = d.get("evaluator", cfg.goal_loop.evaluator)

    d = section("template_compiler")
    if d is not None:
        cfg.template_compiler.loop_infer = d.get("loop_infer", cfg.template_compiler.loop_infer)
        if "cache" in d:
            cfg.template_compiler.cache = as_bool(d["cache"], "template_compiler.cache")

    d = section("tools")
    if d is not None:
        cfg.tools.search_limit = int(d.get("search_limit", cfg.tools.search_limit))
        cfg.tools.max_description_chars = int(
            d.get("max_description_chars", cfg.tools.max_description_chars)
        )
        cfg.tools.entries = d.get("entries", cfg.tools.entries)

    d = section("display")
    if d is not None:
        cfg.display.level = d.get("level", cfg.display.level)

    d = section("session")
    if d is not None:
        cfg.session.dir = d.get("dir", cfg.session.dir)
        cfg.session.workspace_dir = d.get("workspace_dir", cfg.session.workspace_dir)

    cfg.max_parallel = int(data.get("max_parallel", cfg.max_parallel))
    cfg.timeout_per_task = float(data.get("timeout_per_task", cfg.timeout_per_task))
    return cfg


def _user_config_path() -> Path:
    return Path(os.path.expanduser("~")) / ".tasker" / "config.json"


def load_config(path: str | Path | None = None, *, overrides: dict[str, Any] | None = None) -> Config:
    cfg = Config()
    if path:
        p = Path(path)
        if not p.exists():
            raise ValueError(f"配置文件不存在: {p}")
    else:
        candidates = [Path.cwd() / "config.json", _user_config_path(), ROOT / "config.json"]
        p = next((c for c in candidates if c.exists()), ROOT / "config.json")
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"配置文件 {p} 解析失败: {e}") from e
        _merge_cfg(cfg, data)
    if overrides:
        _merge_cfg(cfg, overrides)
    return cfg.validate()


def save_example_config(path: str | Path) -> None:
    example = {
        "llm": {
            "provider": "anthropic",
            "base_url": "",
            "api_key_env": "ANTHROPIC_API_KEY",
            "model": "claude-sonnet-5",
            "temperature": 0.0,
            "max_tokens": 8000,
            "timeout": 120,
        },
        "claude": {
            "model": "",
            "permission_mode": "default",
            "allowed_tools": [],
            "disallowed_tools": [],
        },
        "codex": {
            "binary": "codex",
            "model": "",
            "sandbox": "workspace-write",
            "extra_args": [],
            "approval_policy": "on-request",
            "completion_idle": 5.0,
        },
        "approval": {"mode": "auto", "default_allow": True, "timeout": 120},
        "retry": {"max_retries": 1, "initial_delay": 1.0, "max_delay": 30.0},
        "review": {"enabled": False, "reviewer_count": 2, "min_confidence": 80, "require_evidence": True},
        "hooks": {"rules": []},
        "runtime": {
            "input_queue_maxsize": 64,
            "event_queue_maxsize": 2048,
            "max_context_chars": 16000,
            "max_dependency_chars": 8000,
            "max_state_chars": 2000,
        },
        "dispatch": {
            "strategy": "codex-first-review",
            "complex_executor": "codex",
            "implementation_executor": "claude",
            "verification_executor": "codex",
            "claude_model": "DeepSeek-v4-pro",
            "codex_model": "ChatGPT-5.6",
            "failover_enabled": True,
            "max_failover_attempts": 1,
        },
        "goal_loop": {"max_iterations": 1, "evaluator": "codex"},
        "template_compiler": {"loop_infer": "llm", "cache": True},
        "tools": {"search_limit": 8, "max_description_chars": 320, "entries": []},
        "display": {"level": "minimal"},
        "session": {"dir": "~/.tasker/sessions", "workspace_dir": "~/.tasker/workspace"},
        "max_parallel": 2,
        "timeout_per_task": 900,
    }
    Path(path).write_text(json.dumps(example, indent=2, ensure_ascii=False), encoding="utf-8")
