from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .formatting import compact_json as _compact

EXECUTORS = ("claude", "codex", "human")
EVENT_KINDS = (
    "thinking",            # 思维链 / 推理
    "text",                # 普通文本输出
    "tool_use",            # 工具调用请求
    "tool_result",         # 工具执行结果
    "permission_request",  # 权限 / 审批请求
    "permission_result",   # 审批结果
    "review_request",      # 人工审查点请求（plan 级 human-in-the-loop）
    "review_result",       # 人工审查决定（含反馈）
    "user_message",        # 注入给执行器的用户消息
    "interaction",         # 其它交互事件（子代理、系统通知等）
    "usage",               # token / cost 统计（详情模式）
    "system",              # 系统事件
    "error",               # 错误
    "result",              # 最终结果
    "raw",                 # 无法解析的原始行
)


@dataclass
class TaskLoop:

    enabled: bool = False
    max_iterations: int = 5
    exit_condition: str = ""
    feedback_prompt: str = ""


@dataclass
class SubTask:

    id: str
    title: str
    description: str
    executor: str = "claude"
    depends_on: list[str] = field(default_factory=list)
    acceptance: str = ""
    tool: str = ""
    context: str = ""
    internal_loop: TaskLoop | None = None


@dataclass
class Plan:
    """LLM 拆分出的整体计划。"""

    objective: str
    tasks: list[SubTask] = field(default_factory=list)
    rationale: str = ""
    raw_llm_output: str = ""
    template: Optional[str] = None
    orchestration: dict = field(default_factory=dict)


@dataclass
class GraphEdge:
    src: str
    dst: str


@dataclass
class CompiledGraph:

    nodes: list[SubTask] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    entry: str = ""
    finish: str = "__end__"

    def node_by_id(self, node_id: str) -> Optional[SubTask]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

@dataclass
class Session:

    session_id: str = ""
    goal: str = ""
    status: str = "running"  # running | goal_achieved | failed | paused | stopped
    iteration: int = 0
    state: dict = field(default_factory=dict)  # 累计上下文：上轮输出/已确认验收点/产物清单
    refs: dict = field(default_factory=dict)  # claude_session_id / codex_thread_id 供 resume
    history: list[dict] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Event:

    kind: str
    source: str  # claude | codex | planner | orchestrator
    text: str = ""
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def summary(self, width: int = 90) -> str:
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
        if self.kind == "review_request":
            return f"👁️ 人工审查: {body}"
        if self.kind == "review_result":
            approved = self.data.get("approved", None)
            head = "✅ 审查通过" if approved is True else ("↩️ 审查驳回" if approved is False else "❔ 审查结果")
            return f"{head} {body}"
        if self.kind == "user_message":
            return f"👤 注入消息 {body}"
        if self.kind == "interaction":
            return f"🔁 交互 {body}"
        if self.kind == "usage":
            return f"📊 用量 {body}"
        if self.kind == "error":
            return f"❌ 错误 {body}"
        if self.kind == "result":
            return f"🏁 结果 {body}"
        return f"[{self.kind}] {body}"


def subtask_to_dict(t: SubTask) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "executor": t.executor,
        "depends_on": list(t.depends_on),
        "acceptance": t.acceptance,
        "tool": t.tool,
        "context": t.context,
        "internal_loop": task_loop_to_dict(t.internal_loop),
    }


def task_loop_to_dict(loop: TaskLoop | None) -> dict | None:
    if loop is None:
        return None
    return {
        "enabled": loop.enabled,
        "max_iterations": loop.max_iterations,
        "exit_condition": loop.exit_condition,
        "feedback_prompt": loop.feedback_prompt,
    }


def task_loop_from_dict(value) -> TaskLoop | None:
    if not isinstance(value, dict):
        return None
    if not bool(value.get("enabled", True)):
        return None
    try:
        max_iterations = max(1, int(value.get("max_iterations", 5)))
    except (TypeError, ValueError):
        max_iterations = 5
    return TaskLoop(
        enabled=True,
        max_iterations=max_iterations,
        exit_condition=str(value.get("exit_condition", "") or ""),
        feedback_prompt=str(value.get("feedback_prompt", "") or ""),
    )


def subtask_from_dict(d: dict) -> SubTask:
    return SubTask(
        id=str(d.get("id", "")),
        title=str(d.get("title", "")),
        description=str(d.get("description", "")),
        executor=str(d.get("executor", "claude")),
        depends_on=list(d.get("depends_on") or []),
        acceptance=str(d.get("acceptance", "")),
        tool=str(d.get("tool", d.get("skill", "")) or ""),
        context=str(d.get("context", "")),
        internal_loop=task_loop_from_dict(d.get("internal_loop", d.get("loop"))),
    )


def graph_to_dict(g: CompiledGraph) -> dict:
    return {
        "nodes": [subtask_to_dict(n) for n in g.nodes],
        "edges": [{"src": e.src, "dst": e.dst} for e in g.edges],
        "entry": g.entry,
        "finish": g.finish,
    }


def graph_from_dict(d: dict) -> CompiledGraph:
    return CompiledGraph(
        nodes=[subtask_from_dict(n) for n in (d.get("nodes") or [])],
        edges=[GraphEdge(src=e.get("src", ""), dst=e.get("dst", "")) for e in (d.get("edges") or [])],
        entry=d.get("entry", ""),
        finish=d.get("finish", "__end__"),
    )


@dataclass
class TaskRun:

    task: SubTask
    events: list[Event] = field(default_factory=list)
    status: str = "pending"
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
