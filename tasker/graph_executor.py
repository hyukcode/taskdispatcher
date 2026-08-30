
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import console
from .approvals import ApprovalBroker
from .config import Config
from .models import CompiledGraph, Event, SubTask, TaskRun
from .runner_base import RunnerBase
from .runner_factory import create_runner


def _topo_layers(nodes: list[SubTask], edges) -> list[list[SubTask]]:
    ids = {n.id for n in nodes}
    by_id = {n.id: n for n in nodes}
    indeg = {n.id: 0 for n in nodes}
    adj: dict[str, list[str]] = {n.id: [] for n in nodes}
    for e in edges:
        if e.src in ids and e.dst in ids and e.src != e.dst:
            indeg[e.dst] += 1
            adj[e.src].append(e.dst)
    remaining = dict(indeg)
    layers: list[list[SubTask]] = []
    while remaining:
        ready = sorted(nid for nid, d in remaining.items() if d == 0)
        if not ready:
            raise ValueError("任务依赖存在环，无法执行")
        layers.append([by_id[nid] for nid in ready])
        for nid in ready:
            remaining.pop(nid, None)
            for nxt in adj[nid]:
                remaining[nxt] = remaining.get(nxt, 0) - 1
    return layers


class GraphExecutor:

    def __init__(
        self,
        cfg: Config,
        graph: CompiledGraph,
        broker: ApprovalBroker,
        *,
        workdir: str | Path,
        emit=None,
        goal: str = "",
        state: dict | None = None,
    ):
        self.cfg = cfg
        self.graph = graph
        self.broker = broker
        self.workdir = Path(workdir)
        self.emit = emit or self._default_emit
        self.goal = goal
        self.state = state if state is not None else {}
        self.workdir.mkdir(parents=True, exist_ok=True)

        self.runs: dict[str, TaskRun] = {}
        self._quit = threading.Event()
        self._active: dict[str, RunnerBase] = {}
        self._active_lock = threading.Lock()

    @staticmethod
    def _default_emit(run: TaskRun | None, event: Event) -> None:
        console.event_line(event.summary(), source=event.source)

    def _emit(self, run: TaskRun | None, event: Event) -> None:
        self.emit(run, event)

    def execute(self) -> list[TaskRun]:
        if not self.graph.nodes:
            return []
        try:
            self._execute_dag()
        finally:
            self._quit.set()
            if self._active:
                self.stop()
        return [self.runs[node.id] for node in self.graph.nodes if node.id in self.runs]

    def stop(self) -> None:
        self._quit.set()
        with self._active_lock:
            runners = list(self._active.values())
        for runner in runners:
            runner.stop()

    def send_message(self, target: str, text: str) -> list[str]:
        if not text:
            return []
        matched: list[str] = []
        with self._active_lock:
            items = list(self._active.items())
        for nid, runner in items:
            task = self.runs[nid].task if nid in self.runs else None
            if task is None:
                continue
            if target == "all" or target == task.executor or target == nid or target == nid.lstrip("t"):
                if runner.send_message(text):
                    matched.append(nid)
        return matched

    @property
    def pending_approval_ids(self) -> list[str]:
        return self.broker.pending_ids

    def _execute_dag(self) -> None:
        layers = _topo_layers(self.graph.nodes, self.graph.edges)
        for layer in layers:
            if self._quit.is_set():
                break
            self._run_layer(layer)

    def _run_layer(self, layer: list[SubTask]) -> None:
        runnable: list[SubTask] = []
        for node in layer:
            blocked = self._blocked_predecessors(node.id)
            if blocked:
                self._skip_node(node, blocked)
            else:
                runnable.append(node)

        human_nodes = [n for n in runnable if n.executor == "human"]
        code_nodes = [n for n in runnable if n.executor != "human"]
        if code_nodes:
            self._run_code_nodes_parallel(code_nodes)
        for node in human_nodes:
            self._run_human_with_rerun(node)

    def _run_code_nodes_parallel(self, nodes: list[SubTask]) -> None:
        try:
            max_workers = max(1, int(getattr(self.cfg, "max_parallel", 1)))
        except (TypeError, ValueError):
            max_workers = 1
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="node") as pool:
            futures = [pool.submit(self._run_code_node, node, "") for node in nodes]
            for future in futures:
                future.result()

    def _blocked_predecessors(self, node_id: str) -> list[str]:
        blocked: list[str] = []
        for predecessor in self._predecessors(node_id):
            run = self.runs.get(predecessor)
            if run is None or run.status != "success":
                blocked.append(predecessor)
        return blocked

    def _skip_node(self, node: SubTask, blocked: list[str]) -> None:
        reason = "前置任务未成功：" + ", ".join(blocked)
        run = TaskRun(task=node, workdir=str(self.workdir), status="skipped", error=reason)
        self.runs[node.id] = run
        console.status_line("↷", f"跳过 {node.id} [{node.executor}]：{reason}", "yellow")
        self._emit(run, Event(kind="error", source="orchestrator", text=reason, data={"skipped": True}))

    def _run_code_node(self, node: SubTask, extra_context: str = "") -> TaskRun:
        workdir = str(self.workdir)
        run = TaskRun(task=node, workdir=workdir)
        self.runs[node.id] = run

        try:
            runner = create_runner(
                node.executor,
                self.cfg,
                run,
                workdir,
                self._emit,
                self._prompt_for(node, extra_context),
                broker=self.broker,
            )
        except ValueError as exc:
            run.status = "failed"
            run.error = str(exc)
            self._emit(run, Event(kind="error", source=node.executor, text=run.error))
            return run

        if node.internal_loop is not None:
            runner.set_internal_loop(node.internal_loop)
        with self._active_lock:
            self._active[node.id] = runner
        console.status_line("▶", f"启动 {node.id} [{node.executor}] {node.title}", "blue")
        if node.description:
            console.dim(f"   任务: {node.description.strip().splitlines()[0][:120]}")
        if node.acceptance:
            console.dim(f"   验收: {node.acceptance.strip().splitlines()[0][:120]}")

        try:
            runner.start()
            deadline = time.time() + self.cfg.timeout_per_task
            while not runner.is_done() and not self._quit.is_set():
                pending = getattr(runner, "pending_approval_ids", None)
                if pending:
                    deadline = max(deadline, time.time() + 60)
                elif time.time() > deadline:
                    run.status = "failed"
                    run.error = f"超时（{self.cfg.timeout_per_task:.0f}s）"
                    runner.stop()
                    break
                time.sleep(0.3)
            runner.finalize()
            if run.status not in ("failed",):
                run.status = "success" if run.exit_code == 0 else "failed"
        except FileNotFoundError as e:
            run.status = "failed"
            run.error = f"找不到可执行文件: {e}"
            self._emit(run, Event(kind="error", source=node.executor, text=f"启动失败: {e}"))
        except Exception as e:  # noqa: BLE001
            run.status = "failed"
            run.error = str(e)
            self._emit(run, Event(kind="error", source=node.executor, text=f"运行异常: {e}"))
        finally:
            runner.stop()
            run.ended_at = time.time()
            with self._active_lock:
                self._active.pop(node.id, None)
            if run.status == "success":
                console.status_line("✓", f"{node.id} [{node.executor}] 完成（{run.duration:.1f}s, ${run.cost_usd:.4f}）", "green")
            else:
                console.status_line("✗", f"{node.id} [{node.executor}] {run.status}: {run.error or '见上方输出'}", "red")
        return run

    def _prompt_for(self, node: SubTask, extra_context: str = "") -> str:
        parts: list[str] = []
        if self.goal:
            parts.append(f"总体目标：{self.goal}")
        parts.append(f"任务 {node.id}: {node.title}")
        parts.append(node.description)
        parts.append(
            f"共享工作目录：{self.workdir}\n"
            "请先读取该目录中前置 agent 已产生的代码和文件，再继续本任务；"
            "不要重复从零分析整个模板。"
        )
        if node.tool:
            parts.append(f"指定工具/技能：{node.tool}\n请将其作为本任务的执行工具提示；如果当前执行器没有该工具，明确报告缺失，不要改为分析模板本身。")
        if node.internal_loop is not None and node.internal_loop.enabled:
            loop = node.internal_loop
            loop_prompt = (
                f"本任务包含内部迭代：最多执行 {loop.max_iterations} 轮。"
                "请在当前任务内部完成检查、修正、重试，不要等待编排器重新启动整个任务图。"
                "如果 executor 使用 Codex App Server，编排器会依据结构化 status 在同一 thread 上开启后续 turn。"
            )
            if loop.exit_condition:
                loop_prompt += f"\n内部循环退出条件：{loop.exit_condition}"
            if loop.feedback_prompt:
                loop_prompt += f"\n迭代要求：{loop.feedback_prompt}"
            parts.append(loop_prompt)
        if node.acceptance:
            parts.append(f"完成标准: {node.acceptance}")
        deps = [d for d in self._predecessors(node.id) if d in self.runs and self.runs[d].output]
        if deps:
            parts.append("\n依赖任务输出（作为上下文）：\n" + "\n---\n".join(self.runs[d].output for d in deps)[:8000])
        dep_ids = [d for d in deps if self.runs[d].status == "success"]
        if dep_ids:
            parts.append(
                "\n注意：前置任务 (" + ", ".join(dep_ids) + ") 的文件产物已落在当前工作目录中，请直接读取使用。"
            )
        if self.state:
            parts.append("\n累计状态：" + json.dumps(self.state, ensure_ascii=False)[:2000])
        if extra_context:
            parts.append(extra_context)
        return "\n\n".join(parts)

    def _predecessors(self, node_id: str) -> list[str]:
        return [e.src for e in self.graph.edges if e.dst == node_id]

    def _run_human_with_rerun(self, node: SubTask) -> None:
        while not self._quit.is_set():
            decision = self._run_human_node(node)
            if decision["approved"]:
                return
            feedback = decision["feedback"] or "需要修改"
            preds = self._predecessors(node.id)
            if not preds:
                console.warn(f"审查点 {node.id} 被驳回，但没有可重跑的上游节点，跳过重跑")
                return
            rerun = False
            for pid in preds:
                pred = self.graph.node_by_id(pid)
                if pred is not None and pred.executor != "human":
                    rerun = True
                    console.status_line("↩", f"审查驳回，重跑上游 {pred.id} [{pred.executor}]", "yellow")
                    self._run_code_node(pred, extra_context=f"[审查驳回反馈] {feedback}\n请据此修改后再执行。")
            if not rerun:
                console.warn(f"审查点 {node.id} 被驳回，但没有可重跑的代码上游节点，跳过重跑")
                return

    def _run_human_node(self, node: SubTask) -> dict:
        req_id = f"review-{node.id}-{int(time.time() * 1000)}"
        content = self._review_content(node)
        req = Event(
            kind="review_request",
            source="human",
            text=content[:400],
            data={"id": req_id, "node": node.id, "title": node.title},
        )
        self._emit(None, req)

        got, approved, feedback = self.broker.wait_decision(req_id, kind="review", run=None, event=req)
        if not got or approved is None:
            approved = bool(self.cfg.approval.default_allow)
            feedback = feedback or "（审查超时，按默认策略处理）"

        self._emit(
            None,
            Event(
                kind="review_result",
                source="human",
                text=feedback,
                data={"id": req_id, "node": node.id, "approved": approved, "feedback": feedback},
            ),
        )
        return {"approved": approved, "feedback": feedback}

    def _review_content(self, node: SubTask) -> str:
        parts = [f"审查点：{node.title}"]
        if node.description:
            parts.append(node.description)
        if node.acceptance:
            parts.append(f"审查标准：{node.acceptance}")
        outs = [self.runs[p].output for p in self._predecessors(node.id) if p in self.runs and self.runs[p].output]
        if outs:
            parts.append("上游产出：\n" + "\n---\n".join(outs)[:6000])
        if self.state:
            parts.append("累计状态：" + json.dumps(self.state, ensure_ascii=False)[:2000])
        return "\n\n".join(parts)
