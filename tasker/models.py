"""核心数据模型：计划、子任务、事件、任务运行记录。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# executor 允许值：claude/codex 交给对应 CLI；human 为「人工审查点」（阻塞等用户决定）
EXECUTORS = ("claude", "codex", "human", "llm")
# 事件种类
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
    # LLM 选择使用的模板名（None 表示未用模板）
    template: Optional[str] = None


@dataclass
class GraphEdge:
    """一条有向边（复刻 LangGraph add_edge 的 (src, dst) 语义）。"""

    src: str
    dst: str


@dataclass
class ConditionalBranch:
    """条件边的一个分支：满足 condition 时走向 dst（dst 可为 __end__）。"""

    condition: str
    dst: str


@dataclass
class ConditionalEdge:
    """条件边（复刻 LangGraph add_conditional_edges 的 from + branches 语义）。"""

    src: str
    branches: list[ConditionalBranch] = field(default_factory=list)


@dataclass
class CompiledGraph:
    """模板/计划编译出的可执行图（LangGraph「样子」，但不依赖 langgraph）。

    nodes 复用 SubTask（executor 可为 claude/codex/human）。
    loop=True 时 executor 沿 loop_back_edges 迭代，直到 exit_condition 满足。
    """

    nodes: list[SubTask] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    conditional_edges: list[ConditionalEdge] = field(default_factory=list)
    entry: str = ""
    finish: str = "__end__"
    loop: bool = False
    loop_back_edges: list[GraphEdge] = field(default_factory=list)
    exit_condition: str = ""

    def node_by_id(self, node_id: str) -> Optional[SubTask]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def successors(self, node_id: str) -> list[str]:
        """普通边的后继。"""
        return [e.dst for e in self.edges if e.src == node_id]

    @property
    def real_node_ids(self) -> list[str]:
        """排除 finish（__end__）等伪节点的真实节点 id 列表。"""
        return [n.id for n in self.nodes]


@dataclass
class Session:
    """一次目标任务的持久化状态，支持续跑直到 goal 达成。"""

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


# ================================================================
#  序列化：SubTask / CompiledGraph 与 dict 互转
#  （session.py 存 plan.json、template_compiler.py 存编译缓存共用）
# ================================================================
def subtask_to_dict(t: SubTask) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "executor": t.executor,
        "depends_on": list(t.depends_on),
        "acceptance": t.acceptance,
        "context": t.context,
    }


def subtask_from_dict(d: dict) -> SubTask:
    return SubTask(
        id=str(d.get("id", "")),
        title=str(d.get("title", "")),
        description=str(d.get("description", "")),
        executor=str(d.get("executor", "claude")),
        depends_on=list(d.get("depends_on") or []),
        acceptance=str(d.get("acceptance", "")),
        context=str(d.get("context", "")),
    )


def graph_to_dict(g: CompiledGraph) -> dict:
    return {
        "nodes": [subtask_to_dict(n) for n in g.nodes],
        "edges": [{"src": e.src, "dst": e.dst} for e in g.edges],
        "conditional_edges": [
            {"src": ce.src, "branches": [{"condition": b.condition, "dst": b.dst} for b in ce.branches]}
            for ce in g.conditional_edges
        ],
        "entry": g.entry,
        "finish": g.finish,
        "loop": g.loop,
        "loop_back_edges": [{"src": e.src, "dst": e.dst} for e in g.loop_back_edges],
        "exit_condition": g.exit_condition,
    }


def graph_from_dict(d: dict) -> CompiledGraph:
    return CompiledGraph(
        nodes=[subtask_from_dict(n) for n in (d.get("nodes") or [])],
        edges=[GraphEdge(src=e.get("src", ""), dst=e.get("dst", "")) for e in (d.get("edges") or [])],
        conditional_edges=[
            ConditionalEdge(
                src=ce.get("src", ""),
                branches=[
                    ConditionalBranch(condition=b.get("condition", ""), dst=b.get("dst", ""))
                    for b in (ce.get("branches") or [])
                ],
            )
            for ce in (d.get("conditional_edges") or [])
        ],
        entry=d.get("entry", ""),
        finish=d.get("finish", "__end__"),
        loop=bool(d.get("loop", False)),
        loop_back_edges=[GraphEdge(src=e.get("src", ""), dst=e.get("dst", "")) for e in (d.get("loop_back_edges") or [])],
        exit_condition=d.get("exit_condition", ""),
    )


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
