
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import console
from .config import Config
from .llm import LLMError, chat
from .models import (
    CompiledGraph,
    ConditionalBranch,
    ConditionalEdge,
    GraphEdge,
    SubTask,
    graph_from_dict,
    graph_to_dict,
    task_loop_from_dict,
)

LOOP_INFER_PROMPT = """\
你是一个任务流程分析器。下面给你一个任务模板的步骤清单，每步含 id / title / description / acceptance。
请判断这些步骤之间是否存在「循环执行（loop）」：即流程是否可能在某个步骤完成后，
回退到更早的步骤重新执行，直到满足某个条件才退出。

判断规则：
1. 重点关注验证 / 测试 / 检查 / 审查类步骤：若其 description 或 acceptance 暗示
   「失败 / 不通过 / 未达标时回到某一步重做」，构成 loop。
2. 也识别任何步骤里明确的重复 / 迭代语义（如「反复…直到…」「循环…」「iterate until…」）。
3. 若存在 loop：给出回边的 from（哪一步回退）、to（回到哪一步），
   以及 exit_condition（什么情况下不再回退、继续往后）。
4. 最多识别一个主要 loop；没有就返回 loop=false。

严格只输出 JSON（不要 markdown 代码块）：
{
  "loop": true,
  "exit_condition": "……",
  "loop_back_edges": [ {"from": "t3", "to": "t2"} ],
  "conditional_edges": [
    {"from": "t3", "branches": [
       {"condition": "测试通过/满足验收", "to": "__end__"},
       {"condition": "测试失败/未达标", "to": "t2"}
    ]}
  ],
  "rationale": "一句话说明判断依据"
}
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        import re

        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError("LLM 返回内容中没有 JSON 对象")
    return json.loads(text[start : end + 1])

def nodes_from_template(template: dict, default_executor: str = "codex") -> list[SubTask]:
    """每条 suggested_tasks → 一个 SubTask；planner 编排结果会覆盖 executor。"""
    suggested = template.get("suggested_tasks") or []
    nodes: list[SubTask] = []
    for i, t in enumerate(suggested, start=1):
        tid = str(t.get("id") or f"t{i}")
        nodes.append(
            SubTask(
                id=tid,
                title=str(t.get("title") or tid),
                description=str(t.get("description") or ""),
                executor=str(t.get("executor") or default_executor),
                depends_on=[],
                acceptance=str(t.get("acceptance") or ""),
                tool=str(t.get("tool") or t.get("skill") or ""),
                internal_loop=task_loop_from_dict(t.get("internal_loop", t.get("loop"))),
            )
        )
    return nodes


def linear_graph(nodes: list[SubTask]) -> CompiledGraph:
    edges = [GraphEdge(src=nodes[i].id, dst=nodes[i + 1].id) for i in range(len(nodes) - 1)]
    entry = nodes[0].id if nodes else ""
    return CompiledGraph(nodes=nodes, edges=edges, entry=entry)


def _steps_text(template: dict) -> str:
    lines = [f"模板名: {template.get('template_name', '')}"]
    suggested = template.get("suggested_tasks") or []
    lines.append("步骤:")
    for i, t in enumerate(suggested, start=1):
        desc = (t.get("description") or "").strip()[:200]
        acc = (t.get("acceptance") or "").strip()[:100]
        lines.append(f"  t{i} {t.get('title', '')} —— 描述: {desc} —— 验收: {acc}")
    ext = template.get("system_prompt_extension", "")
    if ext:
        lines.append(f"补充上下文: {str(ext)[:500]}")
    return "\n".join(lines)


def _infer_loop(cfg: Config, template: dict) -> dict:
    user = _steps_text(template)
    try:
        raw = chat(
            cfg.llm,
            [
                {"role": "system", "content": LOOP_INFER_PROMPT},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
        )
        return _extract_json(raw)
    except (LLMError, json.JSONDecodeError, ValueError, KeyError) as e:
        console.warn(f"模板 loop 判断 LLM 失败（{e}），退化为线性 DAG")
        return {"loop": False, "exit_condition": "", "loop_back_edges": [], "conditional_edges": [], "rationale": ""}


def apply_loop(graph: CompiledGraph, info: dict) -> CompiledGraph:
    if not info.get("loop"):
        return graph

    ids = {n.id for n in graph.nodes}

    cond_edges: list[ConditionalEdge] = []
    back_edges: list[GraphEdge] = []
    for ce in info.get("conditional_edges") or []:
        src = str(ce.get("from", ""))
        branches = [
            ConditionalBranch(condition=str(b.get("condition", "")), dst=str(b.get("to", "")))
            for b in (ce.get("branches") or [])
        ]
        cond_edges.append(ConditionalEdge(src=src, branches=branches))

    for be in info.get("loop_back_edges") or []:
        back_edges.append(GraphEdge(src=str(be.get("from", "")), dst=str(be.get("to", ""))))

    exit_condition = str(info.get("exit_condition", "") or "")
    loop_sources = {edge.src for edge in back_edges}
    loop_sources.update(
        edge.src
        for edge in cond_edges
        if any(branch.dst != "__end__" for branch in edge.branches)
    )
    for node in graph.nodes:
        if node.id in loop_sources and node.internal_loop is None:
            node.internal_loop = task_loop_from_dict(
                {
                    "enabled": True,
                    "max_iterations": 5,
                    "exit_condition": exit_condition,
                    "feedback_prompt": "若未满足退出条件，请在当前任务内继续修正并重试。",
                }
            )

    graph.conditional_edges = cond_edges
    graph.loop_back_edges = back_edges
    graph.loop = False
    graph.exit_condition = exit_condition
    return graph


def _cache_dir() -> Path:
    d = Path.home() / ".tasker" / "compiled"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(template: dict) -> str:
    payload = json.dumps(
        {
            "name": template.get("template_name", ""),
            "ext": template.get("system_prompt_extension", ""),
            "tasks": template.get("suggested_tasks", []),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _load_cache(key: str) -> CompiledGraph | None:
    f = _cache_dir() / f"{key}.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return graph_from_dict(data.get("compiled", {}))
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(key: str, graph: CompiledGraph) -> None:
    try:
        f = _cache_dir() / f"{key}.json"
        f.write_text(
            json.dumps({"compiled": graph_to_dict(graph)}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def compile_template(
    cfg: Config,
    template: dict,
    *,
    use_cache: bool | None = None,
    loop_info: dict | None = None,
) -> CompiledGraph:
    nodes = nodes_from_template(template, default_executor=cfg.dispatch.complex_executor)
    if not nodes:
        return CompiledGraph()

    cache = cfg.template_compiler.cache if use_cache is None else use_cache
    key = _cache_key(template)

    if cache and loop_info is None:
        cached = _load_cache(key)
        if cached is not None and cached.nodes:
            return cached

    if loop_info is not None:
        graph = apply_loop(linear_graph(nodes), loop_info)
    elif cfg.mock or cfg.template_compiler.loop_infer == "off":
        graph = linear_graph(nodes)
    else:
        graph = apply_loop(linear_graph(nodes), _infer_loop(cfg, template))

    if cache and loop_info is None:
        _save_cache(key, graph)
    return graph
