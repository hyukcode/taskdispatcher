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

# 模板顶层已知字段（这些字段有特殊处理逻辑，不会重复透传）
_TPL_KNOWN_FIELDS = frozenset({
    "template_name",
    "source_file",
    "system_prompt_extension",
    "suggested_tasks",
})

# suggested_tasks 条目中的已知字段
_TASK_KNOWN_FIELDS = frozenset({
    "title",
    "description",
    "executor",
    "depends_on",
    "acceptance",
})


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


def plan_with_llm(prompt: str, cfg: Config, emit, template: dict | None = None) -> Plan:
    """调用 LLM 拆分任务。emit 用于记录 planner 侧事件。

    模板发现（无需手动 --template）：
    1. 从 ~/.tasker/templates/ 读取已存储的模板目录
    2. 将目录注入 system prompt，LLM 看到可用模板后自行决定参考哪个
    3. 若显式传入 template，同时注入该模板的完整内容
    """
    sys_prompt = SYSTEM_PROMPT
    user_content = f"目标：\n{prompt}"

    # —— 自动发现模板目录 ——
    catalog = _load_catalog()
    if catalog:
        sys_prompt += (
            "\n\n## 可用模板目录\n"
            "以下是本地已存储的任务拆解模板。如果当前目标与某个模板相关，"
            "请参考其任务结构进行拆分（可以调整、合并或增删）：\n\n"
            + catalog
        )

    # —— 显式模板：注入完整内容 ——
    if template:
        sys_prompt, user_content = _inject_template(sys_prompt, user_content, template)

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_content},
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


def _load_catalog() -> str:
    """从本地模板库读取目录摘要，供 LLM 决策使用。"""
    try:
        from template import catalog_for_llm

        return catalog_for_llm()
    except ImportError:
        return ""
    except Exception:
        return ""


def _inject_template(sys_prompt: str, user_content: str, template: dict) -> tuple[str, str]:
    """将显式模板的完整内容注入 prompt。"""
    ext = template.get("system_prompt_extension", "")
    if ext:
        sys_prompt = sys_prompt + "\n\n## 选中的模板详情\n" + ext[:3000]

    suggested = template.get("suggested_tasks") or []
    if suggested:
        lines = ["\n\n## 参考模板（以下为建议的任务拆分，请以此为起点进行调整）"]
        for i, t in enumerate(suggested, start=1):
            lines.append(
                f"{i}. [{t.get('executor', 'claude')}] {t.get('title', '')}"
                f"  ← {', '.join(t.get('depends_on', [])) or '—'}"
            )
            desc = t.get("description", "")
            if desc:
                lines.append(f"   {desc[:300]}")
            # —— 透传 suggested_tasks 中的未知字段 ——
            extra = {k: v for k, v in t.items() if k not in _TASK_KNOWN_FIELDS}
            if extra:
                lines.append(f"   元信息: {json.dumps(extra, ensure_ascii=False)}")
        user_content += "\n".join(lines)

    # —— 透传：模板中的未知字段全部注入 system prompt ——
    passthrough = {k: v for k, v in template.items() if k not in _TPL_KNOWN_FIELDS}
    if passthrough:
        sys_prompt += (
            "\n\n## 模板附加元信息（透传字段）\n"
            + json.dumps(passthrough, ensure_ascii=False, indent=2)[:2000]
        )

    return sys_prompt, user_content


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
