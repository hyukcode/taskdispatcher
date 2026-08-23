"""模板编译：把普通模板（suggested_tasks 线性列表）编译成可执行图。

节点机械生成：每条 suggested_tasks → SubTask(id=t1..tn，executor 占位 claude)。
loop 判断：把步骤清单交给 LLM（规则写在 LOOP_INFER_PROMPT 里），返回固定 JSON
  {loop, exit_condition, loop_back_edges, conditional_edges, rationale}，
  覆盖到默认线性 DAG 上（conditional edge 取代对应 src 的普通出边）。
缓存：编译结果写到 ~/.tasker/compiled/，按模板内容 hash 键控，二次编译直接读。
兜底：LLM 失败 → loop=false 线性 DAG（漏判的 loop 由外层 goal loop + evaluator 兜住）。

模板格式不动（tp-wy 包零改动）。
"""

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
    """从 LLM 返回文本中提取 JSON 对象（与 planner._extract_json 同逻辑）。"""
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


# ================================================================
#  节点 / 线性图（机械生成）
# ================================================================
def nodes_from_template(template: dict) -> list[SubTask]:
    """每条 suggested_tasks → 一个 SubTask（executor 占位 claude，由 planner 阶段覆盖）。"""
    suggested = template.get("suggested_tasks") or []
    nodes: list[SubTask] = []
    for i, t in enumerate(suggested, start=1):
        tid = str(t.get("id") or f"t{i}")
        nodes.append(
            SubTask(
                id=tid,
                title=str(t.get("title") or tid),
                description=str(t.get("description") or ""),
                executor="claude",
                depends_on=[],
                acceptance=str(t.get("acceptance") or ""),
            )
        )
    return nodes


def linear_graph(nodes: list[SubTask]) -> CompiledGraph:
    """默认无 loop：线性边 t1→t2→…→tn，末节点无出边即终止。"""
    edges = [GraphEdge(src=nodes[i].id, dst=nodes[i + 1].id) for i in range(len(nodes) - 1)]
    entry = nodes[0].id if nodes else ""
    return CompiledGraph(nodes=nodes, edges=edges, entry=entry)


# ================================================================
#  loop 推断（LLM）
# ================================================================
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
    """调用 LLM 判断 loop，返回结构化信息；失败返回 loop=false。"""
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


# ================================================================
#  loop 覆盖
# ================================================================
def apply_loop(graph: CompiledGraph, info: dict) -> CompiledGraph:
    """把 LLM 返回的 loop 信息覆盖到图上。

    conditional edge 取代其 src 的普通出边；loop_back_edges / exit_condition 记录。
    """
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
        # 移除 src 的普通出边（改由 conditional edge 路由）
        graph.edges = [e for e in graph.edges if e.src != src]

    for be in info.get("loop_back_edges") or []:
        back_edges.append(GraphEdge(src=str(be.get("from", "")), dst=str(be.get("to", ""))))

    graph.conditional_edges = cond_edges
    graph.loop_back_edges = back_edges
    graph.loop = True
    graph.exit_condition = str(info.get("exit_condition", ""))
    return graph


# ================================================================
#  缓存
# ================================================================
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


# ================================================================
#  入口
# ================================================================
def compile_template(cfg: Config, template: dict, *, use_cache: bool | None = None) -> CompiledGraph:
    """普通模板 → 可执行图（含 loop 结构）。"""
    nodes = nodes_from_template(template)
    if not nodes:
        return CompiledGraph()

    cache = cfg.template_compiler.cache if use_cache is None else use_cache
    key = _cache_key(template)

    if cache:
        cached = _load_cache(key)
        if cached is not None and cached.nodes:
            return cached

    if cfg.template_compiler.loop_infer == "off":
        graph = linear_graph(nodes)
    else:
        graph = apply_loop(linear_graph(nodes), _infer_loop(cfg, template))

    if cache:
        _save_cache(key, graph)
    return graph
