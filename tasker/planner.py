
from __future__ import annotations

import json

from . import console
from .config import Config
from .formatting import extract_json_object
from .llm import LLMError, chat
from .models import (
    EXECUTORS,
    WORKSPACE_ACCESS_MODES,
    WORKDIR_SCOPES,
    Event,
    Plan,
    SubTask,
    is_valid_id,
    resolve_workspace_access,
    resolve_workdir_scope,
    task_loop_from_dict,
)
from .tool_catalog import ToolCatalog
from .workflow import normalize_workflow


REACT_SYSTEM_PROMPT = """\
你是一个采用 ReAct 风格进行任务规划的多智能体任务编排器。
你只负责规划，不执行代码、不调用外部工具，也不要输出思维链；每一轮只输出严格 JSON，
其中 observation 只写简短、可审计的规划结论。

规划动作分三步：
1. decompose：把总体目标拆成最小的、可独立验收的任务；
2. review：检查遗漏、依赖关系、并行机会、executor 分配和验收标准，并给出修订后的完整 tasks；
3. finalize：根据候选任务和复核结果收敛为最终任务图。

最终 tasks 必须使用以下字段：
{"id":"t1","title":"…","description":"可被单独交给 agent 执行的详细指令",
 "tool":"可选工具或 skill","executor":"claude|codex|human","depends_on":[],
 "workspace_access":"read_only|write","workdir_scope":"session|repository",
 "acceptance":"完成标准","internal_loop":{"enabled":false,"max_iterations":5,"exit_condition":""}}

要求：
- id 从 t1 开始递增；
- 互相独立的任务尽量并行，有先后关系才填写 depends_on；
- 代码实现、文件编辑优先 claude；分析、调研、测试、验证优先 codex；
- 发布、生产部署、删除数据、提交主分支和最终验收等高风险步骤使用 human；
- 读取项目、调研、分析、方案设计、测试、验证和代码实现使用 workdir_scope=repository；
- 每个任务必须有非空 description 和 acceptance；
- workspace_access 必须显式填写；无法确定时填写 write，不要根据文字自行推断；
- review/finalize 阶段必须返回完整 tasks，不能只返回修改项；
- 不要新增无法从目标推导出的工作，也不要把整个目标重复塞进多个任务。
"""


def _dispatch_profile_text(cfg: Config) -> str:
    d = cfg.dispatch
    profile = f"""## 实际执行器能力
- claude（实际后端模型：{d.claude_model}）：代码实现、多文件编辑、重构、修复和落地变更。
- codex（实际后端模型：{d.codex_model}）：复杂推理、需求分析、跨仓库阅读、检索、查询计划、测试和结果复核。
- human：高风险操作、不可逆操作和最终人工确认。

当前分派策略：{d.strategy}。复杂分析/检索/验证优先使用 codex；明确的代码落地使用 claude；
代码实现后如有验证步骤，优先交给 codex 复核。不要为了轮换而强行切换 agent，按任务能力分配。"""
    return profile + "\n\n" + ToolCatalog.from_config(cfg).prompt_catalog()


_TEMPLATE_ORCHESTRATION_PROMPT = """\
你是模板任务编排器。模板已经定义了完整的任务清单，不能把它当成普通需求重新拆分。

你的职责只有三项：
1. 为每个已有任务选择 executor；
2. 判断已有任务之间的 depends_on；
3. 判断某个任务是否需要在该任务内部迭代；不要让整个任务图回退重跑。

严格约束：
- 不得新增、删除、合并任务；
- 模板定义的总体目标、任务 id、title、description、acceptance 和工作目录约束是只读契约，不得修改；
- tool/skill 只是工具提示，不属于只读契约；可以保留、清空，或根据当前 executor 实际可用能力替换为其它工具；
- 只返回模板中已有 id 的 executor、depends_on、可选 tool 和 internal_loop；
- 复杂分析、检索、测试、验证优先 codex；代码实现、文件编辑优先 claude；
- 代码实现后的测试/审查优先 codex；高风险操作使用 human；
- 如果模板只有顺序步骤且没有显式依赖，按原顺序建立线性依赖；
- 只有某个任务本身包含“反复执行直到满足条件”的语义时，才在该任务上设置 internal_loop。

严格只输出 JSON（不要 markdown 代码块）：
{
  "tasks": [
    {"id":"t1","executor":"codex","depends_on":[],"tool":"可选的实际可用工具","internal_loop":{"enabled":false,"max_iterations":5,"exit_condition":""}}
  ]
}
"""


def resolve_executor(value: object, cfg: Config) -> str:
    """读取任务显式 executor；缺失或非法值使用配置中的默认 executor。"""
    normalized = str(value or "").strip().lower()
    if normalized in EXECUTORS:
        return normalized
    default = str(getattr(cfg.dispatch, "complex_executor", "codex") or "codex").strip().lower()
    return default if default in EXECUTORS else "codex"


def _normalize_loop_info(data: dict, ids: set[str]) -> dict:
    raw = data.get("loop") if isinstance(data.get("loop"), dict) else {}
    if not raw.get("loop"):
        return {"loop": False, "exit_condition": "", "loop_back_edges": [], "conditional_edges": []}

    back_edges = []
    for edge in raw.get("loop_back_edges") or []:
        src = str(edge.get("from", edge.get("src", "")))
        dst = str(edge.get("to", edge.get("dst", "")))
        if src in ids and dst in ids:
            back_edges.append({"from": src, "to": dst})

    conditional_edges = []
    for edge in raw.get("conditional_edges") or []:
        src = str(edge.get("from", edge.get("src", "")))
        if src not in ids:
            continue
        branches = []
        for branch in edge.get("branches") or []:
            dst = str(branch.get("to", branch.get("dst", "")))
            if dst in ids or dst == "__end__":
                branches.append({"condition": str(branch.get("condition", "")), "to": dst})
        if branches:
            conditional_edges.append({"from": src, "branches": branches})

    if not back_edges and not conditional_edges:
        return {"loop": False, "exit_condition": "", "loop_back_edges": [], "conditional_edges": []}
    return {
        "loop": True,
        "exit_condition": str(raw.get("exit_condition", "")),
        "loop_back_edges": back_edges,
        "conditional_edges": conditional_edges,
    }


def _template_orchestration(prompt: str, cfg: Config, template: dict, emit) -> tuple[dict, str]:
    suggested = template.get("suggested_tasks") or []
    task_lines = []
    for i, task in enumerate(suggested, start=1):
        task_lines.append(
            f"{task.get('id', f't{i}')}: title={task.get('title', '')}; "
            f"description={task.get('description', '')}; tool={task.get('tool') or task.get('skill') or ''}; "
            f"acceptance={task.get('acceptance', '')}"
        )
    user = (
        f"总体目标：{prompt}\n\n模板：{template.get('template_name', '未命名')}\n"
        f"\n任务清单：\n" + "\n".join(task_lines)
    )
    workflow = template.get("workflow", template.get("workflow_stages"))
    if workflow:
        user += "\n\n可选阶段工作流（不得改写任务契约）：\n" + json.dumps(workflow, ensure_ascii=False)[:2500]
    messages = [
        {"role": "system", "content": _TEMPLATE_ORCHESTRATION_PROMPT + "\n\n" + _dispatch_profile_text(cfg)},
        {"role": "user", "content": user},
    ]
    emit(Event(kind="interaction", source="planner", text="调用模板编排 LLM（分配 executor、依赖和 loop）"))
    raw = chat(cfg.llm, messages, temperature=0.0)
    data = _extract_json(raw)
    if not isinstance(data, dict):
        raise LLMError("模板编排 LLM 返回的不是 JSON 对象")
    return data, raw


def _extract_json(text: str) -> dict:
    try:
        return extract_json_object(text)
    except ValueError as exc:
        raise LLMError(f"LLM 返回内容中没有合法 JSON 对象: {exc}") from exc


def _plan_from_llm_data(prompt: str, cfg: Config, data: dict, raw: str) -> Plan:
    """把通用规划 LLM 的结构化结果统一转换为 Plan。"""
    raw_tasks = data.get("tasks", [])
    if not isinstance(raw_tasks, list):
        raise LLMError("任务拆分结果的 tasks 必须是数组")
    tasks = []
    seen: set[str] = set()
    for i, t in enumerate(raw_tasks, start=1):
        if not isinstance(t, dict):
            raise LLMError(f"任务 t{i} 必须是 JSON 对象")
        tid = str(t.get("id") or f"t{i}")
        if tid in seen:
            raise LLMError(f"任务 ID 重复: {tid}")
        seen.add(tid)
        executor = resolve_executor(t.get("executor"), cfg)
        raw_deps = t.get("depends_on", [])
        if raw_deps is None:
            raw_deps = []
        if not isinstance(raw_deps, list):
            raise LLMError(f"任务 {tid} 的 depends_on 必须是数组")
        deps = [str(d) for d in raw_deps]
        tool = str(t.get("tool") or t.get("skill") or "")
        workspace_access = resolve_workspace_access(t)
        workdir_scope = resolve_workdir_scope(t)
        tasks.append(
            SubTask(
                id=tid,
                title=str(t.get("title") or tid),
                description=str(t.get("description") or ""),
                executor=executor,
                depends_on=deps,
                acceptance=str(t.get("acceptance") or ""),
                tool=tool,
                workspace_access=workspace_access,
                workdir_scope=workdir_scope,
                internal_loop=task_loop_from_dict(t.get("internal_loop", t.get("loop"))),
            )
        )
    tpl_name = data.get("template")
    if not isinstance(tpl_name, str) or not tpl_name.strip() or tpl_name.lower() == "null":
        tpl_name = None
    else:
        tpl_name = tpl_name.strip()
    plan = Plan(
        objective=str(data.get("objective") or prompt[:120]),
        rationale=str(data.get("rationale") or data.get("observation") or ""),
        tasks=tasks,
        raw_llm_output=raw,
        template=tpl_name,
    )
    _validate(plan)
    return plan


def _react_step(cfg: Config, system: str, instruction: str, phase: str) -> tuple[dict, str]:
    """执行一个 ReAct 规划阶段，并确保阶段返回完整任务列表。"""
    raw = chat(
        cfg.llm,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": instruction},
        ],
        temperature=0.0,
    )
    data = _extract_json(raw)
    if not isinstance(data.get("tasks"), list) or not data.get("tasks"):
        raise LLMError(f"ReAct {phase} 没有返回完整 tasks")
    return data, raw


def plan_with_react(prompt: str, cfg: Config, emit) -> Plan:
    """无模板时用多轮结构化 ReAct 规划，避免一次性生成未经复核的任务图。"""
    system = REACT_SYSTEM_PROMPT + "\n\n" + _dispatch_profile_text(cfg)
    emit(Event(kind="interaction", source="planner", text="ReAct 拆分：分析目标并生成候选任务"))
    draft, _ = _react_step(
        cfg,
        system,
        f"动作：decompose\n总体目标：\n{prompt}",
        "decompose",
    )

    emit(Event(kind="interaction", source="planner", text="ReAct 拆分：复核依赖、并行关系和验收标准"))
    review, _ = _react_step(
        cfg,
        system,
        (
            "动作：review\n"
            f"总体目标：\n{prompt}\n\n"
            "候选计划：\n"
            f"{json.dumps(draft, ensure_ascii=False)[:12000]}\n\n"
            "检查遗漏、依赖、并行机会、executor、工作目录和验收标准，返回修订后的完整 tasks。"
        ),
        "review",
    )

    emit(Event(kind="interaction", source="planner", text="ReAct 拆分：收敛最终任务图"))
    final, final_raw = _react_step(
        cfg,
        system,
        (
            "动作：finalize\n"
            f"总体目标：\n{prompt}\n\n"
            "候选计划：\n"
            f"{json.dumps(draft, ensure_ascii=False)[:9000]}\n\n"
            "复核后的计划：\n"
            f"{json.dumps(review, ensure_ascii=False)[:12000]}\n\n"
            "请只返回最终完整任务图，保留必要任务，修正依赖和验收标准。"
        ),
        "finalize",
    )
    return _plan_from_llm_data(prompt, cfg, final, final_raw)


def plan_with_single_code_agent(prompt: str, cfg: Config, *, reason: str = "") -> Plan:
    """拆分 LLM 不可用时，不再猜测拆分，交给一个代码 agent 完成整个目标。"""
    executor = cfg.dispatch.implementation_executor
    if executor not in ("claude", "codex"):
        executor = "claude"
    suffix = f"\n\n拆分器状态：{reason}" if reason else ""
    task = SubTask(
        id="t1",
        title="执行完整目标",
        description=(
            f"请直接完成以下总体目标：\n{prompt}\n\n"
            "请先检查当前代码仓库和已有实现，制定必要的内部步骤并直接落地；"
            "完成后运行适当的测试或验证，并汇报实际产出。"
            f"{suffix}"
        ),
        executor=executor,
        workspace_access="write",
        workdir_scope="repository",
        acceptance="总体目标已完成，代码/文件已落地，并提供必要的测试或验证结果",
    )
    plan = Plan(
        objective=prompt[:120],
        rationale=f"拆分 LLM 不可用，整个目标交给 {executor} 执行",
        tasks=[task],
    )
    _validate(plan)
    return plan


def plan_with_llm(prompt: str, cfg: Config, emit, template: dict | None = None) -> Plan:

    if template is None:
        template = _auto_match_template(prompt)
    if template and template.get("suggested_tasks"):
        template = dict(template)
        template.pop("_meta", None)
        try:
            allocation, raw = _template_orchestration(prompt, cfg, template, emit)
            plan = _plan_from_template(prompt, template, allocation=allocation, raw_llm_output=raw, cfg=cfg)
            _validate(plan)
            return plan
        except (LLMError, json.JSONDecodeError, ValueError, KeyError) as e:
            raise LLMError(f"模板编排 LLM 不可用: {e}") from e

    if not template or not template.get("suggested_tasks"):
        return plan_with_react(prompt, cfg, emit)


def _auto_match_template(prompt: str) -> dict | None:
    try:
        from template import get_template, search_templates
    except ImportError:
        return None

    keywords: set[str] = set()
    keywords.update(_keyword_runs(prompt, _is_cjk_character))
    keywords.update(_keyword_runs(prompt, _is_ascii_letter, min_length=3, lower=True))

    ordered = sorted(keywords, key=lambda value: (-len(value), value))
    for keyword in ordered:
        for meta in search_templates(keyword=keyword) or []:
            name = str(meta.get("name") or "")
            if not name:
                continue
            template = get_template(name)
            if template and template.get("suggested_tasks"):
                template = dict(template)
                template.pop("_meta", None)
                return template
    return None


def _is_cjk_character(character: str) -> bool:
    return "\u3400" <= character <= "\u4dbf" or "\u4e00" <= character <= "\u9fff"


def _is_ascii_letter(character: str) -> bool:
    return character.isascii() and character.isalpha()


def _keyword_runs(
    text: str,
    predicate,
    *,
    min_length: int = 1,
    lower: bool = False,
) -> list[str]:
    """扫描连续字符片段，替代模板匹配所需的两类简单正则。"""
    result: list[str] = []
    current: list[str] = []
    for character in text:
        if predicate(character):
            current.append(character)
            continue
        if len(current) >= min_length:
            value = "".join(current)
            result.append(value.lower() if lower else value)
        current = []
    if len(current) >= min_length:
        value = "".join(current)
        result.append(value.lower() if lower else value)
    return result


def _normalize_task_allocation(data: dict, ids: set[str]) -> tuple[dict[str, dict], dict]:
    allocations: dict[str, dict] = {}
    for item in data.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id", ""))
        if tid not in ids:
            continue
        row: dict = {}
        executor = str(item.get("executor", ""))
        if executor in EXECUTORS:
            row["executor"] = executor
        if "depends_on" in item:
            deps = item.get("depends_on") or []
            row["depends_on"] = [str(dep) for dep in deps if str(dep) in ids and str(dep) != tid]
        if "tool" in item or "skill" in item:
            raw_tool = item.get("tool") if "tool" in item else item.get("skill")
            row["tool"] = str(raw_tool or "")
        loop = item.get("internal_loop", item.get("loop"))
        if isinstance(loop, dict):
            row["internal_loop"] = loop
        allocations[tid] = row
    return allocations, _normalize_loop_info(data, ids)


def _plan_from_template(
    prompt: str,
    template: dict,
    *,
    allocation: dict | None = None,
    raw_llm_output: str = "",
    cfg: Config | None = None,
) -> Plan:
    suggested = template["suggested_tasks"]
    effective_cfg = cfg or Config()
    allocation_map, loop_info = _normalize_task_allocation(allocation, {
        str(t.get("id") or f"t{i + 1}") for i, t in enumerate(suggested)
    }) if allocation else ({}, {"loop": False, "exit_condition": "", "loop_back_edges": [], "conditional_edges": []})
    tasks: list[SubTask] = []
    id_set: set[str] = set()
    for i, t in enumerate(suggested):
        tid = str(t.get("id") or f"t{i + 1}")
        id_set.add(tid)
    workflow = normalize_workflow(
        template.get("workflow", template.get("workflow_stages")),
        id_set,
    )
    for i, t in enumerate(suggested):
        tid = str(t.get("id") or f"t{i + 1}")
        selected = allocation_map.get(tid, {})
        executor = resolve_executor(selected.get("executor") or t.get("executor"), effective_cfg)
        if "depends_on" in selected:
            deps = selected["depends_on"]
        else:
            deps = [d for d in (t.get("depends_on") or []) if d in id_set]
        if "tool" in selected:
            tool = str(selected.get("tool") or "")
        else:
            tool = str(t.get("tool") or t.get("skill") or "")
        loop_value = selected.get("internal_loop", t.get("internal_loop", t.get("loop")))
        tasks.append(SubTask(
            id=tid,
            title=str(t.get("title") or tid),
            description=str(t.get("description") or t.get("title", "")),
            executor=executor,
            depends_on=deps,
            acceptance=str(t.get("acceptance") or ""),
            tool=tool,
            workspace_access=resolve_workspace_access(t),
            workdir_scope=resolve_workdir_scope(t),
            internal_loop=task_loop_from_dict(loop_value),
        ))

    if loop_info.get("loop"):
        exit_condition = str(loop_info.get("exit_condition", "") or "")
        for edge in loop_info.get("loop_back_edges") or []:
            source = str(edge.get("from", edge.get("src", "")))
            node = next((item for item in tasks if item.id == source), None)
            if node is not None and node.internal_loop is None:
                node.internal_loop = task_loop_from_dict(
                    {
                        "enabled": True,
                        "max_iterations": 5,
                        "exit_condition": exit_condition,
                        "feedback_prompt": "若未满足退出条件，请在当前任务内继续修正并重试。",
                    }
                )

    if tasks and not any(task.depends_on for task in tasks):
        for previous, current in zip(tasks, tasks[1:]):
            current.depends_on = [previous.id]

    return Plan(
        objective=template.get("template_name") or prompt[:120],
        rationale=(
            f"模板拆分 + {effective_cfg.dispatch.strategy} 编排 "
            f"（来源: {template.get('source_file', '?')}）"
        ),
        tasks=tasks,
        raw_llm_output=raw_llm_output,
        template=template.get("template_name"),
        orchestration={
            "tasks": allocation_map,
            "loop": loop_info,
            "workflow": workflow,
        },
    )


def _validate(plan: Plan) -> None:
    if not isinstance(plan, Plan):
        raise LLMError("计划必须是 Plan")
    if not plan.tasks:
        raise LLMError("计划至少包含一个任务")
    if not isinstance(plan.tasks, list) or not all(isinstance(task, SubTask) for task in plan.tasks):
        raise LLMError("计划 tasks 必须是 SubTask 数组")
    for task in plan.tasks:
        if not isinstance(task.id, str):
            raise LLMError(f"任务 ID 必须是字符串: {task.id!r}")
    ids = {t.id for t in plan.tasks}
    if len(ids) != len(plan.tasks):
        duplicates = sorted({task.id for task in plan.tasks if sum(item.id == task.id for item in plan.tasks) > 1})
        raise LLMError(f"任务 ID 重复: {', '.join(duplicates)}")
    for t in plan.tasks:
        if not is_valid_id(t.id):
            raise LLMError(f"任务 {t.id!r} 的 ID 只能包含字母、数字、下划线和连字符，且不能以符号开头")
        if t.executor not in EXECUTORS:
            raise LLMError(f"任务 {t.id} 的 executor 非法: {t.executor}")
        if t.workspace_access not in WORKSPACE_ACCESS_MODES:
            raise LLMError(f"任务 {t.id} 的 workspace_access 非法: {t.workspace_access}")
        if t.workdir_scope not in WORKDIR_SCOPES:
            raise LLMError(f"任务 {t.id} 的 workdir_scope 非法: {t.workdir_scope}")
        if not isinstance(t.depends_on, list) or not all(isinstance(dep, str) for dep in t.depends_on):
            raise LLMError(f"任务 {t.id} 的 depends_on 必须是字符串数组")
        if len(t.depends_on) != len(set(t.depends_on)):
            raise LLMError(f"任务 {t.id} 的 depends_on 存在重复项")
        for d in t.depends_on:
            if d not in ids:
                raise LLMError(f"任务 {t.id} 依赖不存在的任务 {d}")
            if d == t.id:
                raise LLMError(f"任务 {t.id} 不能依赖自身")
        if not t.description.strip():
            raise LLMError(f"任务 {t.id} 缺少 description")

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
