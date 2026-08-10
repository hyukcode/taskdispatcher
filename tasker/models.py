"""核心数据模型：计划、子任务、事件、任务运行记录。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# executor 允许值
EXECUTORS = ("claude", "codex", "llm")
# 事件种类
EVENT_KINDS = (
    "thinking",            # 思维链 / 推理
    "text",                # 普通文本输出
    "tool_use",            # 工具调用请求
    "tool_result",         # 工具执行结果
    "permission_request",  # 权限 / 审批请求
    "permission_result",   # 审批结果
    "user_message",        # 注入给执行器的用户消息
    "interaction",         # 其它交互事件（子代理、系统通知等）
    "system",              # 系统事件
    "error",               # 错误
    "result",              # 最终结果
    "raw",                 # 无法解析的原始行
)


@dataclass
class SubTask:
    """一个可交给 claude / codex 执行的子任务。"""

    id: str
    title: str
    description: str
    executor: str = "claude"
    depends_on: list[str] = field(default_factory=list)
    acceptance: str = ""
    # 注入执行器 prompt 的额外上下文（由调度器填充前置任务输出）
    context: str = ""


@dataclass
class Plan:
    """LLM 拆分出的整体计划。"""

    objective: str
    tasks: list[SubTask] = field(default_factory=list)
    rationale: str = ""
    raw_llm_output: str = ""


@dataclass
class Event:
    """一条采集到的事件（思维链 / 工具调用 / 审批等）。"""

    kind: str
    source: str  # claude | codex | planner | orchestrator
    text: str = ""
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def summary(self, width: int = 90) -> str:
        """生成一行用于控制台/报告概览的摘要。"""
        body = self.text.strip().replace("\n", " ")
        if len(body) > width:
            body = body[: width - 1] + "…"
        if self.kind == "thinking":
            return f"🧠 思考 {body}"
        if self.kind == "text":
            return f"💬 {body}"
        if self.kind == "tool_use":
            name = self.data.get("tool", self.data.get("name", "?"))
            inp = self.data.get("input") or {}
            return f"🔧 工具调用 {name}: {_compact(inp, width)}"
        if self.kind == "tool_result":
            return f"📥 工具结果 {body}"
        if self.kind == "permission_request":
            return f"🛡️ 审批请求: {body}"
        if self.kind == "permission_result":
            allow = self.data.get("allowed", None)
            head = "✅ 已批准" if allow is True else ("⛔ 已拒绝" if allow is False else "❔ 审批结果")
            return f"{head} {body}"
        if self.kind == "user_message":
            return f"👤 注入消息 {body}"
        if self.kind == "interaction":
            return f"🔁 交互 {body}"
        if self.kind == "error":
            return f"❌ 错误 {body}"
        if self.kind == "result":
            return f"🏁 结果 {body}"
        return f"[{self.kind}] {body}"


def _compact(obj, width: int) -> str:
    import json

    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    return s if len(s) <= width else s[: width - 1] + "…"


@dataclass
class TaskRun:
    """一次子任务执行记录（含全部事件）。"""

    task: SubTask
    events: list[Event] = field(default_factory=list)
    status: str = "pending"  # pending | running | success | failed | skipped
    output: str = ""
    exit_code: Optional[int] = None
    error: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    cost_usd: float = 0.0
    raw_log_path: str = ""
    workdir: str = ""

    @property
    def duration(self) -> float:
        return (self.ended_at - self.started_at) if self.ended_at else 0.0

    @property
    def thinking_events(self) -> list[Event]:
        return [e for e in self.events if e.kind == "thinking"]

    @property
    def permission_events(self) -> list[Event]:
        return [e for e in self.events if e.kind in ("permission_request", "permission_result")]

    @property
    def tool_events(self) -> list[Event]:
        return [e for e in self.events if e.kind in ("tool_use", "tool_result")]
