"""模板可选的阶段化工作流元数据。"""

from __future__ import annotations

from .models import CompiledGraph, GraphEdge


def normalize_workflow(raw, task_ids: set[str]) -> list[dict]:
    """校验并规范化模板阶段；未声明的任务自动归入最后阶段。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("stages", [])
    if not isinstance(raw, list):
        raise ValueError("模板 workflow 必须是 stages 数组或对象")

    stages: list[dict] = []
    used: set[str] = set()
    stage_ids: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"模板 workflow 第 {index} 项必须是对象")
        stage_id = str(item.get("id") or item.get("name") or f"stage-{index}").strip()
        name = str(item.get("name") or stage_id).strip()
        if not stage_id or stage_id in stage_ids:
            raise ValueError(f"模板 workflow 阶段 ID 非法或重复: {stage_id}")
        raw_tasks = item.get("task_ids", item.get("tasks", []))
        if isinstance(raw_tasks, str):
            raw_tasks = [raw_tasks]
        if not isinstance(raw_tasks, list) or not all(isinstance(task_id, str) for task_id in raw_tasks):
            raise ValueError(f"模板 workflow 阶段 {stage_id} 的 tasks 必须是字符串数组")
        task_list = [task_id.strip() for task_id in raw_tasks if task_id.strip()]
        unknown = sorted(set(task_list) - task_ids)
        if unknown:
            raise ValueError(f"模板 workflow 阶段 {stage_id} 引用了不存在的任务: {unknown}")
        duplicate = sorted(set(task_list).intersection(used))
        if duplicate:
            raise ValueError(f"模板 workflow 任务重复分配: {duplicate}")
        stage_ids.add(stage_id)
        used.update(task_list)
        stages.append(
            {
                "id": stage_id,
                "name": name,
                "task_ids": task_list,
                "purpose": str(item.get("purpose", "") or "")[:1000],
                "requires_review": bool(item.get("requires_review", False)),
            }
        )

    remaining = [task_id for task_id in task_ids if task_id not in used]
    if remaining:
        stage_id = "stage-remaining"
        while stage_id in stage_ids:
            stage_id += "-next"
        stages.append(
            {
                "id": stage_id,
                "name": "未指定阶段任务",
                "task_ids": sorted(remaining),
                "purpose": "模板未显式分组的任务",
                "requires_review": False,
            }
        )
    return stages


def apply_workflow_barriers(graph: CompiledGraph, workflow: list[dict]) -> None:
    """为阶段之间增加屏障依赖，保证后一阶段不会提前执行。"""
    if not workflow:
        return
    by_id = {node.id: node for node in graph.nodes}
    edge_pairs = {(edge.src, edge.dst) for edge in graph.edges}
    previous: list[str] = []
    for stage in workflow:
        current = [task_id for task_id in stage.get("task_ids", []) if task_id in by_id]
        for task_id in current:
            node = by_id[task_id]
            for dependency in previous:
                if dependency == task_id:
                    continue
                if dependency not in node.depends_on:
                    node.depends_on.append(dependency)
                pair = (dependency, task_id)
                if pair not in edge_pairs:
                    graph.edges.append(GraphEdge(src=dependency, dst=task_id))
                    edge_pairs.add(pair)
        previous.extend(current)


def stage_for_task(workflow: list[dict], task_id: str) -> dict | None:
    for stage in workflow:
        if task_id in stage.get("task_ids", []):
            return stage
    return None
