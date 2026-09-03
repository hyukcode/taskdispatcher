from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .formatting import compact_json as _compact

EXECUTORS = ("claude", "codex", "human")
WORKSPACE_ACCESS_MODES = ("read_only", "write")
WORKDIR_SCOPES = ("session", "repository")
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
    "retry",               # 同一执行器的有限重试或跨执行器故障转移
    "usage",               # token / cost 统计（详情模式）
    "system",              # 系统事件
    "error",               # 错误
    "result",              # 最终结果
    "raw",                 # 无法解析的原始行
)


def is_valid_id(value: object) -> bool:
    """判断任务/会话 ID 是否只包含安全的 ASCII 标识字符。"""
    if not isinstance(value, str) or not value:
        return False
    first = value[0]
    if not first.isascii() or not first.isalnum():
        return False
    return all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in value[1:]
    )


def resolve_workspace_access(data: dict) -> str:
    """读取任务显式声明；缺失或非法值一律按 write 处理。"""
    value = str(data.get("workspace_access") or "").strip().lower()
    return value if value in WORKSPACE_ACCESS_MODES else "write"


def resolve_workdir_scope(data: dict) -> str:
    """读取任务显式工作目录；缺失或非法值按 session 处理。

    ``repository`` 布尔字段只作为旧模板格式的显式兼容字段，不参与文本推断。
    """
    value = str(data.get("workdir_scope") or "").strip().lower()
    if value in WORKDIR_SCOPES:
        return value
    if "repository" in data:
        return "repository" if bool(data.get("repository")) else "session"
    return "session"


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
    # 默认按可写任务处理，只有明确声明 read_only 才允许同层并发。
    workspace_access: str = "write"
    # session：中间产物目录；repository：用户启动 tasker 的代码仓库目录。
    workdir_scope: str = "session"
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
    # 模板可选的阶段信息；不改变任务契约，只增加执行屏障和展示元数据。
    workflow: list[dict] = field(default_factory=list)

    def node_by_id(self, node_id: str) -> Optional[SubTask]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

@dataclass
class Session:

    session_id: str = ""
    goal: str = ""
    status: str = "running"  # running | goal_achieved | failed | paused | stopped | deleted
    iteration: int = 0
    state: dict = field(default_factory=dict)  # 累计上下文：上轮输出/已确认验收点/产物清单
    # 可选 runner 引用；当前恢复以 plan_signature/task_runs 的任务级检查点为准。
    refs: dict = field(default_factory=dict)
    # 当前已持久化计划的签名和任务快照，用于 /resume 跳过已成功任务。
    plan_signature: str = ""
    task_runs: dict[str, dict] = field(default_factory=dict)
    deleted_at: str = ""
    deleted_from_status: str = ""
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
        if self.kind == "retry":
            return f"🔄 重试 {body}"
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
        "workspace_access": t.workspace_access,
        "workdir_scope": t.workdir_scope,
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
    if not isinstance(d, dict):
        raise ValueError("任务必须是 JSON 对象")
    raw_dependencies = d.get("depends_on", [])
    if raw_dependencies is None:
        raw_dependencies = []
    if not isinstance(raw_dependencies, list) or not all(isinstance(item, str) for item in raw_dependencies):
        raise ValueError("任务 depends_on 必须是字符串数组")
    workspace_access = resolve_workspace_access(d)
    workdir_scope = resolve_workdir_scope(d)
    return SubTask(
        id=str(d.get("id", "")),
        title=str(d.get("title", "")),
        description=str(d.get("description", "")),
        executor=str(d.get("executor", "claude")),
        depends_on=list(raw_dependencies),
        acceptance=str(d.get("acceptance", "")),
        tool=str(d.get("tool", d.get("skill", "")) or ""),
        context=str(d.get("context", "")),
        workspace_access=workspace_access,
        workdir_scope=workdir_scope,
        internal_loop=task_loop_from_dict(d.get("internal_loop", d.get("loop"))),
    )


def graph_to_dict(g: CompiledGraph) -> dict:
    return {
        "nodes": [subtask_to_dict(n) for n in g.nodes],
        "edges": [{"src": e.src, "dst": e.dst} for e in g.edges],
        "entry": g.entry,
        "finish": g.finish,
        "workflow": [dict(stage) for stage in g.workflow],
    }


def graph_from_dict(d: dict) -> CompiledGraph:
    if not isinstance(d, dict):
        raise ValueError("任务图必须是 JSON 对象")
    raw_nodes = d.get("nodes", [])
    raw_edges = d.get("edges", [])
    raw_workflow = d.get("workflow", [])
    if raw_nodes is None:
        raw_nodes = []
    if raw_edges is None:
        raw_edges = []
    if raw_workflow is None:
        raw_workflow = []
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list) or not isinstance(raw_workflow, list):
        raise ValueError("任务图的 nodes、edges 和 workflow 必须是数组")
    if not all(isinstance(node, dict) for node in raw_nodes):
        raise ValueError("任务图 nodes 中存在非对象项")
    if not all(isinstance(edge, dict) for edge in raw_edges):
        raise ValueError("任务图 edges 中存在非对象项")
    if not all(isinstance(stage, dict) for stage in raw_workflow):
        raise ValueError("任务图 workflow 中存在非对象项")
    return CompiledGraph(
        nodes=[subtask_from_dict(n) for n in raw_nodes],
        edges=[GraphEdge(src=str(e.get("src", "")), dst=str(e.get("dst", ""))) for e in raw_edges],
        entry=str(d.get("entry", "") or ""),
        finish=str(d.get("finish", "__end__") or "__end__"),
        workflow=[dict(stage) for stage in raw_workflow],
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
    # 每次执行尝试都有独立 ID；task.id 在故障转移和 /continue 中保持不变。
    attempt_id: str = ""
    parent_attempt_id: str = ""
    failure_class: str = ""
    retryable: bool = False
    # 同一任务发生执行器故障转移时，按时间顺序保存每次尝试的摘要。
    attempts: list[dict] = field(default_factory=list)

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


def task_run_to_dict(run: TaskRun) -> dict:
    """保存可恢复任务所需的最小快照；事件正文仍由 JSONL 日志保存。"""
    return {
        "task": subtask_to_dict(run.task),
        "status": run.status,
        "output": run.output,
        "exit_code": run.exit_code,
        "error": run.error,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "cost_usd": run.cost_usd,
        "raw_log_path": run.raw_log_path,
        "workdir": run.workdir,
        "attempt_id": run.attempt_id,
        "parent_attempt_id": run.parent_attempt_id,
        "failure_class": run.failure_class,
        "retryable": run.retryable,
        "attempts": [dict(item) for item in run.attempts],
    }


def task_run_from_dict(data: dict, task: SubTask | None = None) -> TaskRun:
    if not isinstance(data, dict):
        raise ValueError("任务运行快照必须是 JSON 对象")
    saved_task = task or subtask_from_dict(data.get("task") or {})
    raw_exit_code = data.get("exit_code")
    raw_attempts = data.get("attempts", [])
    if raw_attempts is None:
        raw_attempts = []
    if not isinstance(raw_attempts, list) or not all(isinstance(item, dict) for item in raw_attempts):
        raise ValueError("任务运行快照的 attempts 必须是对象数组")
    try:
        exit_code = None if raw_exit_code is None else int(raw_exit_code)
    except (TypeError, ValueError) as exc:
        raise ValueError("任务运行快照的 exit_code 非法") from exc
    return TaskRun(
        task=saved_task,
        status=str(data.get("status", "pending")),
        output=str(data.get("output", "") or ""),
        exit_code=exit_code,
        error=str(data.get("error", "") or ""),
        started_at=float(data.get("started_at", 0.0) or 0.0),
        ended_at=float(data.get("ended_at", 0.0) or 0.0),
        cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
        raw_log_path=str(data.get("raw_log_path", "") or ""),
        workdir=str(data.get("workdir", "") or ""),
        attempt_id=str(data.get("attempt_id", "") or ""),
        parent_attempt_id=str(data.get("parent_attempt_id", "") or ""),
        failure_class=str(data.get("failure_class", "") or ""),
        retryable=bool(data.get("retryable", False)),
        attempts=[dict(item) for item in raw_attempts],
    )


def validate_graph(graph: CompiledGraph) -> None:
    """验证从磁盘或外部 planner 得到的图，防止脏数据进入执行器。"""
    if not isinstance(graph, CompiledGraph):
        raise ValueError("任务图必须是 CompiledGraph")
    if not isinstance(graph.nodes, list) or not isinstance(graph.edges, list):
        raise ValueError("任务图的 nodes 和 edges 必须是数组")
    ids: set[str] = set()
    for node in graph.nodes:
        if not isinstance(node, SubTask):
            raise ValueError("任务图 nodes 中存在非 SubTask 项")
        if not is_valid_id(node.id):
            raise ValueError(f"任务 ID 非法: {node.id!r}")
        if node.id in ids:
            raise ValueError(f"任务 ID 重复: {node.id}")
        ids.add(node.id)
        if node.executor not in EXECUTORS:
            raise ValueError(f"任务 {node.id} 的 executor 非法: {node.executor}")
        if not isinstance(node.workspace_access, str) or node.workspace_access not in WORKSPACE_ACCESS_MODES:
            raise ValueError(f"任务 {node.id} 的 workspace_access 非法: {node.workspace_access}")
        if not isinstance(node.workdir_scope, str) or node.workdir_scope not in WORKDIR_SCOPES:
            raise ValueError(f"任务 {node.id} 的 workdir_scope 非法: {node.workdir_scope}")
        if not isinstance(node.description, str) or not node.description.strip():
            raise ValueError(f"任务 {node.id} 缺少 description")
        if not isinstance(node.depends_on, list) or not all(isinstance(item, str) for item in node.depends_on):
            raise ValueError(f"任务 {node.id} 的 depends_on 必须是字符串数组")

    for node in graph.nodes:
        if len(node.depends_on) != len(set(node.depends_on)):
            raise ValueError(f"任务 {node.id} 的 depends_on 存在重复项")
        for dependency in node.depends_on:
            if dependency not in ids:
                raise ValueError(f"任务 {node.id} 依赖不存在的任务 {dependency}")
            if dependency == node.id:
                raise ValueError(f"任务 {node.id} 不能依赖自身")

    if not isinstance(graph.entry, str) or not isinstance(graph.finish, str):
        raise ValueError("任务图的 entry 和 finish 必须是字符串")
    if graph.nodes and not graph.entry:
        raise ValueError("非空任务图必须指定 entry")
    if graph.entry and graph.entry not in ids:
        raise ValueError(f"图入口不存在: {graph.entry}")

    if not isinstance(graph.workflow, list) or not all(isinstance(stage, dict) for stage in graph.workflow):
        raise ValueError("任务图 workflow 必须是对象数组")
    workflow_tasks: set[str] = set()
    workflow_stage_ids: set[str] = set()
    for stage in graph.workflow:
        stage_id = str(stage.get("id", "") or "")
        raw_task_ids = stage.get("task_ids", [])
        if not stage_id or stage_id in workflow_stage_ids:
            raise ValueError(f"任务图 workflow 阶段 ID 非法或重复: {stage_id}")
        if not isinstance(raw_task_ids, list) or not all(isinstance(task_id, str) for task_id in raw_task_ids):
            raise ValueError(f"任务图 workflow 阶段 {stage_id} 的 task_ids 必须是字符串数组")
        for task_id in raw_task_ids:
            if task_id not in ids:
                raise ValueError(f"任务图 workflow 引用不存在的任务: {task_id}")
            if task_id in workflow_tasks:
                raise ValueError(f"任务图 workflow 任务重复分配: {task_id}")
            workflow_tasks.add(task_id)
        workflow_stage_ids.add(stage_id)

    seen_edges: set[tuple[str, str]] = set()
    declared_dependencies = {(dependency, node.id) for node in graph.nodes for dependency in node.depends_on}
    indegree = {node_id: 0 for node_id in ids}
    adjacency = {node_id: [] for node_id in ids}
    for edge in graph.edges:
        if not isinstance(edge, GraphEdge) or not isinstance(edge.src, str) or not isinstance(edge.dst, str):
            raise ValueError("任务图 edges 中存在非法边")
        pair = (edge.src, edge.dst)
        if edge.src not in ids or edge.dst not in ids:
            raise ValueError(f"图边引用不存在的任务: {edge.src} -> {edge.dst}")
        if edge.src == edge.dst:
            raise ValueError(f"任务不能依赖自身: {edge.src}")
        if pair in seen_edges:
            raise ValueError(f"图边重复: {edge.src} -> {edge.dst}")
        seen_edges.add(pair)
        indegree[edge.dst] += 1
        adjacency[edge.src].append(edge.dst)

    # 计划节点的 depends_on 是对外声明，CompiledGraph.edges 是执行器实际使用的边。
    # 无 depends_on 的旧版线性图仍可兼容；一旦显式声明依赖，两者必须完全一致。
    if declared_dependencies and seen_edges != declared_dependencies:
        missing = sorted(declared_dependencies - seen_edges)
        extra = sorted(seen_edges - declared_dependencies)
        raise ValueError(f"任务依赖声明与图边不一致: missing={missing}, extra={extra}")

    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for nxt in adjacency[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    if visited != len(ids):
        raise ValueError("任务依赖存在环")
