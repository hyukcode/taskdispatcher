
from __future__ import annotations

import json
import logging

from . import console
from .config import Config
from .formatting import extract_json_object
from .llm import LLMError, chat
from .models import Event, Plan, SubTask, infer_workspace_access, infer_workdir_scope, is_valid_id, task_loop_from_dict
from .tool_catalog import ToolCatalog
from .workflow import normalize_workflow


logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
你是一个多智能体任务编排器。用户会给你一个目标，请把它拆分为若干个可并行/串行的子任务，
并分派给 claude（擅长代码实现、文件编辑、整体工程）、codex（擅长推理、测试、批处理）或 human（人工审查点）。
要求：
- 每个子任务依赖其它子任务时，用 depends_on 声明（引用其它任务的 id）。
- 让互相独立的子任务尽量并行（分给不同的 executor）。
- workdir_scope=repository 用于读取项目、调研、分析、方案设计、测试、验证和代码实现；workdir_scope=session 只用于日志、事件和明确的中间产物。
- 输出严格 JSON（不要 markdown 代码块），格式：
{
  "objective": "目标一句话",
  "rationale": "为什么这样拆分，一句话",
  "template": "使用的模板名（未使用模板则为 null）",
  "tasks": [
    {"id":"t1","title":"…","description":"详细指令，可被单独交给一个 agent 执行","tool":"可选工具或 skill","executor":"claude|codex|human","depends_on":[],"workspace_access":"read_only|write","workdir_scope":"session|repository","acceptance":"完成标准","internal_loop":{"enabled":false,"max_iterations":5,"exit_condition":""}}
  ]
}
- 只输出 JSON。id 从 t1 开始递增。

人工审查点（executor:"human"）识别规则：
若某一步骤满足以下任一条件，应把其 executor 设为 "human"（人工审查点，会阻塞等待用户决定）：
1. 涉及对外发布、部署到生产、提交到主分支、删除数据等不可逆/高风险操作；
2. 关键决策点（技术选型、方案定稿、是否上线的拍板）；
3. 需要人核对产出是否正确/是否符合预期（最终验收、内容审校、上线前检查）。
其它步骤在 claude / codex 中二选一。human 步骤的 description 写清「审查什么」，
acceptance 写清「通过标准」。
"""

_TPL_KNOWN_FIELDS = frozenset({
    "template_name",
    "source_file",
    "system_prompt_extension",
    "suggested_tasks",
    "workflow",
    "workflow_stages",
})

_TASK_KNOWN_FIELDS = frozenset({
    "title",
    "description",
    "executor",
    "depends_on",
    "acceptance",
    "tool",
    "skill",
    "workspace_access",
    "workdir_scope",
    "internal_loop",
    "loop",
})


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


def _fallback_executor(task: dict, cfg: Config) -> str:
    text = " ".join(
        str(task.get(k) or "") for k in ("title", "description", "tool", "acceptance")
    ).lower()
    if any(k in text for k in ("发布", "部署到生产", "删除数据", "提交主分支", "上线", "approve")):
        return "human"
    if any(k in text for k in (
        "读取", "阅读", "提取", "分析", "计划", "查询", "查找", "搜索", "检索", "测试", "验证",
        "评估", "调研", "研究", "总结", "比较", "indexer", "research", "analy", "search", "test",
    )):
        return cfg.dispatch.complex_executor
    if any(k in text for k in (
        "实现", "编写", "修改", "修复", "重构", "编辑", "落地", "开发", "增加", "删除",
        "implement", "edit", "fix", "refactor", "build",
    )):
        return cfg.dispatch.implementation_executor
    return cfg.dispatch.complex_executor


def _enforce_executor(task: dict, proposed: str, cfg: Config) -> str:
    text = " ".join(
        str(task.get(k) or "") for k in ("title", "description", "tool", "acceptance")
    ).lower()
    if any(k in text for k in ("发布", "部署到生产", "删除数据", "提交主分支", "上线", "approve")):
        return "human"
    if proposed == "human":
        return "human"
    if any(k in text for k in (
        "实现", "编写", "修改", "修复", "重构", "编辑", "落地", "开发", "增加",
        "implement", "edit", "fix", "refactor", "build",
    )):
        return cfg.dispatch.implementation_executor
    if any(k in text for k in (
        "读取", "阅读", "提取", "分析", "计划", "查询", "查找", "搜索", "检索", "测试", "验证",
        "评估", "调研", "研究", "总结", "比较", "indexer", "research", "analy", "search", "test",
    )):
        return cfg.dispatch.complex_executor
    if proposed in ("claude", "codex"):
        return proposed
    return cfg.dispatch.complex_executor


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
            console.warn(f"模板编排 LLM 失败（{e}），使用 Codex-first 能力分配")
            fallback = _plan_from_template(prompt, template, cfg=cfg)
            _validate(fallback)
            return fallback

    sys_prompt = SYSTEM_PROMPT + "\n\n" + _dispatch_profile_text(cfg)
    user_content = f"目标：\n{prompt}"

    if not template:
        catalog = _load_catalog()
        if catalog:
            sys_prompt += (
                "\n\n## 可用模板目录\n"
                "以下是本地已存储的任务拆解模板。如果当前目标与某个模板相关，"
                "请参考其任务结构进行拆分（可以调整、合并或增删）：\n\n"
                + catalog
            )

    if template:
        sys_prompt, user_content = _inject_template(sys_prompt, user_content, template, cfg=cfg)

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_content},
    ]
    emit(Event(kind="interaction", source="planner", text="调用任务拆分 LLM（模型 " + cfg.llm.model + "）"))
    raw = chat(cfg.llm, messages, temperature=0.0)
    data = _extract_json(raw)
    raw_tasks = data.get("tasks", [])
    if not isinstance(raw_tasks, list):
        raise LLMError("任务拆分结果的 tasks 必须是数组")
    tasks = []
    seen: set[str] = set()
    raw_dependencies: list[list[str]] = []
    for i, t in enumerate(raw_tasks, start=1):
        if not isinstance(t, dict):
            raise LLMError(f"任务 t{i} 必须是 JSON 对象")
        tid = str(t.get("id") or f"t{i}")
        if tid in seen:
            raise LLMError(f"任务 ID 重复: {tid}")
        seen.add(tid)
        executor = _enforce_executor(t, str(t.get("executor") or ""), cfg)
        raw_deps = t.get("depends_on", [])
        if raw_deps is None:
            raw_deps = []
        if not isinstance(raw_deps, list):
            raise LLMError(f"任务 {tid} 的 depends_on 必须是数组")
        deps = [str(d) for d in raw_deps]
        tool = str(t.get("tool") or t.get("skill") or "")
        workspace_access = infer_workspace_access(t)
        workdir_scope = infer_workdir_scope(t, enforce_repository_semantics=True)
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
        raw_dependencies.append(deps)
    for task, deps in zip(tasks, raw_dependencies):
        task.depends_on = deps
    tpl_name = data.get("template")
    if not isinstance(tpl_name, str) or not tpl_name.strip() or tpl_name.lower() == "null":
        tpl_name = None
    else:
        tpl_name = tpl_name.strip()
    plan = Plan(
        objective=str(data.get("objective") or prompt[:120]),
        rationale=str(data.get("rationale") or ""),
        tasks=tasks,
        raw_llm_output=raw,
        template=tpl_name,
    )
    _validate(plan)
    return plan


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


def _load_catalog() -> str:
    try:
        from template import catalog_for_llm

        return catalog_for_llm()
    except ImportError:
        return ""
    except Exception:
        logger.debug("加载模板目录失败", exc_info=True)
        return ""


def _inject_template(sys_prompt: str, user_content: str, template: dict, cfg: Config | None = None) -> tuple[str, str]:

    import json as _json

    tpl_name = template.get("template_name", "未命名模板")
    ext = template.get("system_prompt_extension", "")
    suggested = template.get("suggested_tasks") or []
    passthrough = {k: v for k, v in template.items() if k not in _TPL_KNOWN_FIELDS}

    parts = [f"## 必用模板：{tpl_name}"]

    if cfg is not None:
        parts.append("\n" + _dispatch_profile_text(cfg))

    if ext:
        parts.append(f"\n### 模板说明\n{ext[:2000]}")

    workflow = template.get("workflow", template.get("workflow_stages"))
    if workflow:
        parts.append(
            "\n### 可选阶段工作流（只控制执行顺序和阶段展示，不得改写任务契约）\n"
            + _json.dumps(workflow, ensure_ascii=False)[:2500]
        )

    if suggested:
        parts.append("\n### 模板任务清单（必须严格遵循，任务数量与标题不得增删）")
        for i, t in enumerate(suggested, start=1):
            tid = t.get("id", f"t{i}")
            title = t.get("title", "")
            desc = t.get("description", "")
            acceptance = t.get("acceptance", "")
            tool = t.get("tool") or t.get("skill") or ""
            workspace_access = infer_workspace_access(t)
            workdir_scope = infer_workdir_scope(t, enforce_repository_semantics=True)
            parts.append(
                f"  {tid}: {title}\n"
                f"    描述: {desc[:500]}\n"
                f"    工具/技能: {tool or '—'}\n"
                f"    工作区访问: {workspace_access}\n"
                f"    工作目录: {workdir_scope}\n"
                f"    验收: {acceptance or '—'}"
            )
            # 透传 suggested_tasks 中的未知字段
            extra = {k: v for k, v in t.items() if k not in _TASK_KNOWN_FIELDS}
            if extra:
                parts.append(f"    元信息: {_json.dumps(extra, ensure_ascii=False)}")

    if passthrough:
        parts.append(
            f"\n### 模板附加约束\n{_json.dumps(passthrough, ensure_ascii=False, indent=2)[:1500]}"
        )

    parts.append(
        "\n**重要约束：你必须以上述模板的任务清单为蓝本进行拆分，保留模板的总体目标、任务 id、"
        "标题、描述、验收标准和工作目录约束，不得增删任务或改写这些内容。**\n"
        "**模板中的 tool/skill 只是任务所需工具提示，可以根据当前 executor 实际可用能力保留、替换或留空；模板不指定 executor（由 claude 还是 codex 执行）与 depends_on（任务间依赖）："
        "请你根据每个任务的性质（如代码实现、测试、调研等）自行分配合适的 executor，"
        "并根据任务间的逻辑先后关系自行填写 depends_on。**"
    )

    sys_prompt += "\n\n" + "\n".join(parts)
    return sys_prompt, user_content


def _rule_split(prompt: str) -> list[SubTask]:
    low = prompt.lower()
    tasks: list[SubTask] = []

    has_code = any(w in low for w in ("代码", "写", "实现", "修复", "bug", "重构", "test", "测试", "build", "函数", "脚本"))
    has_research = any(w in low for w in ("分析", "研究", "搜索", "调研", "web", "找资料", "调查", "比较", "对比", "阅读", "检索", "评估", "方案", "架构"))

    research_id = ""
    if has_research:
        research_id = f"t{len(tasks) + 1}"
        tasks.append(SubTask(id=research_id, title="调研与资料收集", description=prompt + "\n\n请先在当前代码仓库中完成调研：阅读项目文件并整理相关资料，输出要点与来源。", executor="codex", workspace_access="read_only", workdir_scope="repository"))

    if has_code:
        design_id = f"t{len(tasks) + 1}"
        tasks.append(
            SubTask(
                id=design_id,
                title="方案设计",
                description=f"为以下目标做技术方案设计（模块划分、接口、依赖）：\n{prompt}",
                executor="codex",
                workdir_scope="repository",
                depends_on=[research_id] if research_id else [],
            )
        )
        implementation_id = f"t{len(tasks) + 1}"
        tasks.append(
            SubTask(
                id=implementation_id,
                title="代码实现",
                description=f"按方案实现：\n{prompt}\n\n直接在当前工作目录中编写并落地代码。",
                executor="claude",
                workdir_scope="repository",
                depends_on=[design_id],
            )
        )
        tasks.append(
            SubTask(
                id=f"t{len(tasks) + 1}",
                title="测试与验证",
                description="对刚实现的代码编写/运行测试，验证功能正确，报告结果。",
                executor="codex",
                workdir_scope="repository",
                depends_on=[implementation_id],
            )
        )
    elif not tasks:
        tasks.append(SubTask(id="t1", title="执行目标", description=prompt + "\n\n在当前工作目录中完成上述目标，并汇报结果。", executor="codex"))
    return tasks


def plan_with_rules(prompt: str, template: dict | None = None) -> Plan:
    if template is None:
        template = _auto_match_template(prompt)
    if template and template.get("suggested_tasks"):
        plan = _plan_from_template(prompt, template)
        _validate(plan)
        return plan
    tasks = _rule_split(prompt)
    plan = Plan(
        objective=prompt[:120],
        rationale="规则拆分（未调用 LLM）",
        tasks=tasks,
        raw_llm_output="",
    )
    _validate(plan)
    return plan


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
        if executor in ("claude", "codex", "human"):
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
        executor = _enforce_executor(
            t,
            str(selected.get("executor") or t.get("executor") or ""),
            effective_cfg,
        )
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
            workspace_access=infer_workspace_access(t),
            workdir_scope=infer_workdir_scope(t, enforce_repository_semantics=True),
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
        tasks=tasks or _rule_split(prompt),
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
        if t.executor not in ("claude", "codex", "human"):
            raise LLMError(f"任务 {t.id} 的 executor 非法: {t.executor}")
        if t.workspace_access not in ("read_only", "write"):
            raise LLMError(f"任务 {t.id} 的 workspace_access 非法: {t.workspace_access}")
        if t.workdir_scope not in ("session", "repository"):
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
