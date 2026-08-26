
from __future__ import annotations

from .models import CompiledGraph, SubTask

_CODE_KEYWORDS = (
    "代码", "实现", "写", "修复", "重构", "编辑", "文件", "构建", "脚本", "函数", "功能", "开发",
    "code", "implement", "fix", "build", "edit", "file", "feature",
)
_REASON_KEYWORDS = (
    "推理", "调研", "搜索", "研究", "分析", "批处理", "总结", "比较", "对比", "测试", "验证", "评估",
    "web", "找资料", "调查", "reasoning", "research", "analyze", "test", "verify", "evaluate",
)


def is_short_circuit(graph: CompiledGraph, min_steps: int = 3) -> bool:
    if not graph.nodes:
        return False
    if graph.loop or any(n.internal_loop is not None for n in graph.nodes):
        return False
    if any(n.executor == "human" for n in graph.nodes):
        return False
    return len(graph.nodes) < min_steps


def choose_executor(nodes: list[SubTask], default: str = "codex") -> str:
    text = " ".join(f"{n.title} {n.description} {n.acceptance}" for n in nodes).lower()
    code_hit = any(k in text for k in _CODE_KEYWORDS)
    reason_hit = any(k in text for k in _REASON_KEYWORDS)
    if code_hit and not reason_hit:
        return "claude"
    if reason_hit and not code_hit:
        return "codex"
    return default


def build_single_task(graph: CompiledGraph, goal: str = "") -> SubTask:
    executor = choose_executor(graph.nodes)
    titles = "；".join(n.title for n in graph.nodes if n.title)
    desc = "\n\n".join(n.description for n in graph.nodes if n.description)
    accs = [n.acceptance for n in graph.nodes if n.acceptance]
    tools = sorted({n.tool for n in graph.nodes if n.tool})
    acceptance = graph.exit_condition or "；".join(accs)
    loops = [n.internal_loop for n in graph.nodes if n.internal_loop is not None]

    head = f"{goal}\n\n" if goal else ""
    body = desc or titles
    if tools:
        body = "指定工具/技能：" + "、".join(tools) + "\n\n" + body
    return SubTask(
        id="t1",
        title=titles or goal or "执行目标",
        description=f"{head}{body}".strip(),
        executor=executor,
        depends_on=[],
        acceptance=acceptance,
        tool="、".join(tools),
        internal_loop=(loops[0] if len(loops) == 1 else None),
    )
