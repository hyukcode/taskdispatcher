"""任务分派策略：单 agent 短路 + executor 选择启发式。

在 GraphExecutor 之前拦截：
- 真实节点数 < min_multiagent_steps 且无 human 节点且无 loop → 折叠为单 agent 直接执行。
- 否则返回 None，交给 GraphExecutor 走多节点图执行。

executor 选择沿用 planner 的启发式：编码/文件编辑 → claude；推理/调研/测试 → codex。
"""
from __future__ import annotations

from .models import CompiledGraph, SubTask

# executor 选择启发式关键词（小写匹配）
_CODE_KEYWORDS = (
    "代码", "实现", "写", "修复", "重构", "编辑", "文件", "构建", "脚本", "函数", "功能", "开发",
    "code", "implement", "fix", "build", "edit", "file", "feature",
)
_REASON_KEYWORDS = (
    "推理", "调研", "搜索", "研究", "分析", "批处理", "总结", "比较", "对比", "测试", "验证", "评估",
    "web", "找资料", "调查", "reasoning", "research", "analyze", "test", "verify", "evaluate",
)


def is_short_circuit(graph: CompiledGraph, min_steps: int = 3) -> bool:
    """小任务短路判定：< min_steps 且无 human 且无 loop。"""
    if not graph.nodes:
        return False
    if graph.loop:
        return False
    if any(n.executor == "human" for n in graph.nodes):
        return False
    return len(graph.nodes) < min_steps


def choose_executor(nodes: list[SubTask], default: str = "claude") -> str:
    """根据节点标题/描述启发式选 executor：编码 → claude；推理/测试 → codex。"""
    text = " ".join(f"{n.title} {n.description} {n.acceptance}" for n in nodes).lower()
    code_hit = any(k in text for k in _CODE_KEYWORDS)
    reason_hit = any(k in text for k in _REASON_KEYWORDS)
    if code_hit and not reason_hit:
        return "claude"
    if reason_hit and not code_hit:
        return "codex"
    return default


def build_single_task(graph: CompiledGraph, goal: str = "") -> SubTask:
    """把整图折叠成一条「完整目标」prompt，交给单个 agent 执行。"""
    executor = choose_executor(graph.nodes)
    titles = "；".join(n.title for n in graph.nodes if n.title)
    desc = "\n\n".join(n.description for n in graph.nodes if n.description)
    accs = [n.acceptance for n in graph.nodes if n.acceptance]
    acceptance = graph.exit_condition or "；".join(accs)

    head = f"{goal}\n\n" if goal else ""
    body = desc or titles
    return SubTask(
        id="t1",
        title=titles or goal or "执行目标",
        description=f"{head}{body}".strip(),
        executor=executor,
        depends_on=[],
        acceptance=acceptance,
    )
