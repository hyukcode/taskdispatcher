"""配置加载：config.json + 环境变量合并。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class LLMConfig:
    """任务拆分用的 LLM 配置（也可直接接入 DeepSeek/OpenAI 等兼容接口）。"""

    provider: str = "anthropic"  # anthropic | openai（openai 为兼容接口，可填任意 base_url）
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
    # default | acceptEdits | bypassPermissions | dontAsk | plan | manual
    # 编排器建议用 acceptEdits：headless 下自动允许工作区内文件编辑；
    # 要完全自主（含 Bash）再改 bypassPermissions。
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    # claude 输出最终 result 后，若无新注入/新活动，等待该秒数后关闭 stdin 收尾
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
    # app-server 模式（--no-app-server 可退回 codex exec）
    use_app_server: bool = True
    # untrusted | on-failure | on-request | never — thread/start 的 approvalPolicy；
    # 建议 on-request 让每个审批都到达 tasker 的审批策略
    approval_policy: str = "on-request"
    # 最终 result 后的静默秒数（同 ClaudeConfig.completion_idle 语义）
    completion_idle: float = 5.0


@dataclass
class ApprovalConfig:
    """审批请求处理方式。"""

    mode: str = "auto"  # auto | log | ask_console | ptty
    default_allow: bool = True
    timeout: float = 120.0


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    workspace_dir: str = "workspaces"
    report_dir: str = "reports"
    max_parallel: int = 2
    timeout_per_task: float = 900.0
    mock: bool = False

    @property
    def workspace_path(self) -> Path:
        # 相对路径相对当前工作目录解析（而非包安装目录），pipx 安装下产物落在用户运行目录
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

    cfg.workspace_dir = data.get("workspace_dir", cfg.workspace_dir)
    cfg.report_dir = data.get("report_dir", cfg.report_dir)
    cfg.max_parallel = int(data.get("max_parallel", cfg.max_parallel))
    cfg.timeout_per_task = float(data.get("timeout_per_task", cfg.timeout_per_task))
    cfg.mock = bool(data.get("mock", cfg.mock))
    return cfg


def load_config(path: str | Path | None = None, *, overrides: dict[str, Any] | None = None) -> Config:
    """加载配置文件。缺省顺序：./config.json（当前目录）→ 包目录 config.json → 内置默认值。overrides 优先于文件。"""
    cfg = Config()
    if path:
        p = Path(path)
    else:
        cwd_cfg = Path.cwd() / "config.json"
        p = cwd_cfg if cwd_cfg.exists() else ROOT / "config.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
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
        "workspace_dir": "workspaces",
        "report_dir": "reports",
        "max_parallel": 2,
        "timeout_per_task": 900,
    }
    Path(path).write_text(json.dumps(example, indent=2, ensure_ascii=False), encoding="utf-8")
