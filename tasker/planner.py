"""任务拆分：把用户 prompt 拆成依赖图，分派给 claude / codex。

两种来源：
- plan_with_llm()：调用 LLM API 拆分（默认）。
- plan_with_rules()：确定性规则拆分（--plan-rules / --mock，无 key 也能跑）。
"""
from __future__ import annotations

import json
import re

from .config import Config
from .llm import LLMError, chat
from .models import Event, Plan, SubTask

SYSTEM_PROMPT = """\
你是一个多智能体任务编排器。用户会给你一个目标，请把它拆分为若干个可并行/串行的子任务，
并分派给 claude（擅长代码实现、文件编辑、整体工程）或 codex（擅长推理、测试、批处理）。
要求：
- 每个子任务依赖其它子任务时，用 depends_on 声明（引用其它任务的 id）。
- 让互相独立的子任务尽量并行（分给不同的 executor）。
- 输出严格 JSON（不要 markdown 代码块），格式：
{
  "objective": "目标一句话",
  "rationale": "为什么这样拆分，一句话",
  "tasks": [
    {"id":"t1","title":"…","description":"详细指令，可被单独交给一个 agent 执行","executor":"claude|codex","depends_on":[],"acceptance":"完成标准"}
  ]
}
- 只输出 JSON。id 从 t1 开始递增。executor 只能是 claude 或 codex。
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError("LLM 返回内容中没有 JSON 对象")
    return json.loads(text[start : end + 1])


def plan_with_llm(prompt: str, cfg: Config, emit) -> Plan:
    """调用 LLM 拆分任务。emit 用于记录 planner 侧事件。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"目标：\n{prompt}"},
    ]
    emit(Event(kind="interaction", source="planner", text="调用任务拆分 LLM（模型 " + cfg.llm.model + "）"))
    raw = chat(cfg.llm, messages, temperature=0.0)
    data = _extract_json(raw)
    tasks = []
    seen = set()
    for i, t in enumerate(data.get("tasks", []), start=1):
        tid = str(t.get("id") or f"t{i}")
        if tid in seen:
            tid = f"t{i}"
        seen.add(tid)
        executor = t.get("executor", "claude")
        if executor not in ("claude", "codex"):
            executor = "claude"
        deps = [d for d in (t.get("depends_on") or []) if isinstance(d, str) and d in seen]
        tasks.append(
            SubTask(
                id=tid,
                title=str(t.get("title") or tid),
                description=str(t.get("description") or ""),
                executor=executor,
                depends_on=deps,
                acceptance=str(t.get("acceptance") or ""),
            )
        )
    plan = Plan(
        objective=str(data.get("objective") or prompt[:120]),
        rationale=str(data.get("rationale") or ""),
        tasks=tasks,
        raw_llm_output=raw,
    )
    _validate(plan)
    return plan


def _rule_split(prompt: str) -> list[SubTask]:
    """确定性规则拆分：无 LLM key / --mock 时使用。"""
    low = prompt.lower()
    tasks: list[SubTask] = []

    wants_both = ("claude" in low or "两个" in prompt) and ("codex" in low or "双" in prompt)
    has_code = any(w in low for w in ("代码", "写", "实现", "修复", "bug", "重构", "test", "测试", "build", "函数", "脚本"))
    has_research = any(w in low for w in ("研究", "搜索", "调研", "web", "找资料", "调查", "比较", "对比"))

    if has_research:
        tasks.append(SubTask(id="t1", title="调研与资料收集", description=prompt + "\n\n请先做调研：搜索并整理相关资料，输出要点与来源。", executor="claude"))

    if has_code:
        tasks.append(
            SubTask(
                id="t2",
                title="方案设计",
                description=f"为以下目标做技术方案设计（模块划分、接口、依赖）：\n{prompt}",
                executor="claude",
                depends_on=["t1"] if has_research else [],
            )
        )
        tasks.append(
            SubTask(
                id="t3",
                title="代码实现",
                description=f"按方案实现：\n{prompt}\n\n直接在当前工作目录中编写并落地代码。",
                executor="codex" if (has_research and wants_both) else "claude",
                depends_on=["t2"],
            )
        )
        tasks.append(
            SubTask(
                id="t4",
                title="测试与验证",
                description="对刚实现的代码编写/运行测试，验证功能正确，报告结果。",
                executor="codex",
                depends_on=["t3"],
            )
        )
    elif not tasks:
        tasks.append(
            SubTask(
                id="t1",
                title="执行目标",
                description=prompt + "\n\n在当前工作目录中完成上述目标，并汇报结果。",
                executor="claude",
            )
        )
    return tasks


def plan_with_rules(prompt: str) -> Plan:
    tasks = _rule_split(prompt)
    return Plan(
        objective=prompt[:120],
        rationale="规则拆分（未调用 LLM）",
        tasks=tasks,
        raw_llm_output="",
    )


def _validate(plan: Plan) -> None:
    """校验依赖引用、executor 合法性、环检测。"""
    ids = {t.id for t in plan.tasks}
    for t in plan.tasks:
        if t.executor not in ("claude", "codex"):
            raise LLMError(f"任务 {t.id} 的 executor 非法: {t.executor}")
        for d in t.depends_on:
            if d not in ids:
                raise LLMError(f"任务 {t.id} 依赖不存在的任务 {d}")
        if not t.description.strip():
            raise LLMError(f"任务 {t.id} 缺少 description")

    # 环检测（Kahn）
    indeg = {t.id: len(t.depends_on) for t in plan.tasks}
    adj = {t.id: [] for t in plan.tasks}
    for t in plan.tasks:
        for d in t.depends_on:
            adj[d].append(t.id)
    import collections

    q = collections.deque([tid for tid, d in indeg.items() if d == 0])
    visited = 0
    while q:
        nid = q.popleft()
        visited += 1
        for nxt in adj[nid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                q.append(nxt)
    if visited != len(plan.tasks):
        raise LLMError("任务依赖存在环")


def levels(plan: Plan) -> list[list[SubTask]]:
    """把任务按依赖拓扑分成若干可并行层。"""
    remaining = {t.id: set(t.depends_on) for t in plan.tasks}
    by_id = {t.id: t for t in plan.tasks}
    out: list[list[SubTask]] = []
    while remaining:
        ready = [tid for tid, deps in remaining.items() if not deps]
        if not ready:
            raise LLMError("任务依赖存在环，无法分层")
        out.append([by_id[tid] for tid in sorted(ready)])
        ready_set = set(ready)
        remaining = {tid: deps - ready_set for tid, deps in remaining.items() if tid not in ready_set}
    return out
