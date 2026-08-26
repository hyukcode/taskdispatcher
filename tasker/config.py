from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


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
    binary: str = "claude"
    model: str = ""
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    completion_idle: float = 5.0
    extra_args: list[str] = field(default_factory=list)


@dataclass
class CodexConfig:
    binary: str = "codex"
    model: str = ""
    # read-only | workspace-write | danger-full-access
    sandbox: str = "workspace-write"
    auto_approve: bool = False
    skip_git_check: bool = True
    full_trace: bool = True
    extra_args: list[str] = field(default_factory=list)
    use_app_server: bool = True
    approval_policy: str = "on-request"
    completion_idle: float = 5.0


@dataclass
class ApprovalConfig:
    """审批请求处理方式。"""

    mode: str = "auto"  # auto | log | ask_console | ptty
    default_allow: bool = True
    timeout: float = 120.0


@dataclass
class DispatchConfig:
    """任务分派策略。"""

    min_multiagent_steps: int = 3
    strategy: str = "codex-first-review"
    complex_executor: str = "codex"
    implementation_executor: str = "claude"
    verification_executor: str = "codex"
    claude_model: str = "DeepSeek-v4-pro"
    codex_model: str = "ChatGPT-5.6"


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
    goal_loop: GoalLoopConfig = field(default_factory=GoalLoopConfig)
    template_compiler: TemplateCompilerConfig = field(default_factory=TemplateCompilerConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    workspace_dir: str = "workspaces"
    report_dir: str = "reports"
    max_parallel: int = 2
    timeout_per_task: float = 900.0
    mock: bool = False

    @property
    def workspace_path(self) -> Path:
        p = Path(self.workspace_dir)
        return p if p.is_absolute() else Path.cwd() / p

    @property
    def report_path(self) -> Path:
        p = Path(self.report_dir)
        return p if p.is_absolute() else Path.cwd() / p


def _merge_cfg(cfg: Config, data: dict[str, Any]) -> Config:
    llm = cfg.llm
    if "llm" in data:
        d = data["llm"]
        llm.provider = d.get("provider", llm.provider)
        llm.base_url = d.get("base_url", llm.base_url)
        llm.api_key_env = d.get("api_key_env", llm.api_key_env)
        llm.model = d.get("model", llm.model)
        llm.temperature = float(d.get("temperature", llm.temperature))
        llm.max_tokens = int(d.get("max_tokens", llm.max_tokens))
        llm.timeout = float(d.get("timeout", llm.timeout))

    if "claude" in data:
        d = data["claude"]
        cfg.claude.binary = d.get("binary", cfg.claude.binary)
        cfg.claude.model = d.get("model", cfg.claude.model)
        cfg.claude.permission_mode = d.get("permission_mode", cfg.claude.permission_mode)
        cfg.claude.allowed_tools = d.get("allowed_tools", cfg.claude.allowed_tools)
        cfg.claude.disallowed_tools = d.get("disallowed_tools", cfg.claude.disallowed_tools)
        cfg.claude.completion_idle = float(d.get("completion_idle", cfg.claude.completion_idle))
        cfg.claude.extra_args = d.get("extra_args", cfg.claude.extra_args)

    if "codex" in data:
        d = data["codex"]
        cfg.codex.binary = d.get("binary", cfg.codex.binary)
        cfg.codex.model = d.get("model", cfg.codex.model)
        cfg.codex.sandbox = d.get("sandbox", cfg.codex.sandbox)
        cfg.codex.auto_approve = bool(d.get("auto_approve", cfg.codex.auto_approve))
        cfg.codex.skip_git_check = bool(d.get("skip_git_check", cfg.codex.skip_git_check))
        cfg.codex.full_trace = bool(d.get("full_trace", cfg.codex.full_trace))
        cfg.codex.extra_args = d.get("extra_args", cfg.codex.extra_args)
        cfg.codex.use_app_server = bool(d.get("use_app_server", cfg.codex.use_app_server))
        cfg.codex.approval_policy = d.get("approval_policy", cfg.codex.approval_policy)
        cfg.codex.completion_idle = float(d.get("completion_idle", cfg.codex.completion_idle))

    if "approval" in data:
        d = data["approval"]
        cfg.approval.mode = d.get("mode", cfg.approval.mode)
        cfg.approval.default_allow = bool(d.get("default_allow", cfg.approval.default_allow))
        cfg.approval.timeout = float(d.get("timeout", cfg.approval.timeout))

    if "dispatch" in data:
        d = data["dispatch"]
        cfg.dispatch.min_multiagent_steps = int(d.get("min_multiagent_steps", cfg.dispatch.min_multiagent_steps))
        cfg.dispatch.strategy = d.get("strategy", cfg.dispatch.strategy)
        cfg.dispatch.complex_executor = d.get("complex_executor", cfg.dispatch.complex_executor)
        cfg.dispatch.implementation_executor = d.get("implementation_executor", cfg.dispatch.implementation_executor)
        cfg.dispatch.verification_executor = d.get("verification_executor", cfg.dispatch.verification_executor)
        cfg.dispatch.claude_model = d.get("claude_model", cfg.dispatch.claude_model)
        cfg.dispatch.codex_model = d.get("codex_model", cfg.dispatch.codex_model)

    if "goal_loop" in data:
        d = data["goal_loop"]
        cfg.goal_loop.max_iterations = int(d.get("max_iterations", cfg.goal_loop.max_iterations))
        cfg.goal_loop.evaluator = d.get("evaluator", cfg.goal_loop.evaluator)

    if "template_compiler" in data:
        d = data["template_compiler"]
        cfg.template_compiler.loop_infer = d.get("loop_infer", cfg.template_compiler.loop_infer)
        cfg.template_compiler.cache = bool(d.get("cache", cfg.template_compiler.cache))

    if "display" in data:
        d = data["display"]
        cfg.display.level = d.get("level", cfg.display.level)

    if "session" in data:
        d = data["session"]
        cfg.session.dir = d.get("dir", cfg.session.dir)
        cfg.session.workspace_dir = d.get("workspace_dir", cfg.session.workspace_dir)

    cfg.workspace_dir = data.get("workspace_dir", cfg.workspace_dir)
    cfg.report_dir = data.get("report_dir", cfg.report_dir)
    cfg.max_parallel = int(data.get("max_parallel", cfg.max_parallel))
    cfg.timeout_per_task = float(data.get("timeout_per_task", cfg.timeout_per_task))
    cfg.mock = bool(data.get("mock", cfg.mock))
    return cfg


def _user_config_path() -> Path:
    return Path(os.path.expanduser("~")) / ".tasker" / "config.json"


def load_config(path: str | Path | None = None, *, overrides: dict[str, Any] | None = None) -> Config:
    cfg = Config()
    if path:
        p = Path(path)
    else:
        candidates = [Path.cwd() / "config.json", _user_config_path(), ROOT / "config.json"]
        p = next((c for c in candidates if c.exists()), ROOT / "config.json")
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"配置文件 {p} 解析失败: {e}") from e
        _merge_cfg(cfg, data)
    if overrides:
        _merge_cfg(cfg, overrides)
    return cfg


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
            "binary": "claude",
            "model": "",
            "permission_mode": "default",
            "allowed_tools": [],
            "disallowed_tools": [],
            "extra_args": [],
        },
        "codex": {
            "binary": "codex",
            "model": "",
            "sandbox": "workspace-write",
            "auto_approve": False,
            "skip_git_check": True,
            "full_trace": True,
            "extra_args": [],
            "use_app_server": True,
            "approval_policy": "on-request",
            "completion_idle": 5.0,
        },
        "approval": {"mode": "auto", "default_allow": True, "timeout": 120},
        "dispatch": {
            "min_multiagent_steps": 3,
            "strategy": "codex-first-review",
            "complex_executor": "codex",
            "implementation_executor": "claude",
            "verification_executor": "codex",
            "claude_model": "DeepSeek-v4-pro",
            "codex_model": "ChatGPT-5.6",
        },
        "goal_loop": {"max_iterations": 1, "evaluator": "codex"},
        "template_compiler": {"loop_infer": "llm", "cache": True},
        "display": {"level": "minimal"},
        "session": {"dir": "~/.tasker/sessions", "workspace_dir": "~/.tasker/workspace"},
        "workspace_dir": "workspaces",
        "report_dir": "reports",
        "max_parallel": 2,
        "timeout_per_task": 900,
    }
    Path(path).write_text(json.dumps(example, indent=2, ensure_ascii=False), encoding="utf-8")
