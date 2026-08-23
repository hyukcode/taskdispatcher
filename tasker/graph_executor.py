"""图执行器：CompiledGraph → 分派 runner 执行，覆盖 DAG / loop / human 三种结构。

取代 Scheduler 的核心职责：
- DAG：按边做拓扑分层，层内并行、层间串行。
- loop：entry → … → 回边起点 反复迭代；每轮用 judge 判定 exit_condition，
  满足则结束，不满足则把反馈注回 loop 起点重跑。
- human：executor=="human" 节点阻塞等待用户决定（走 ApprovalBroker 的 review 类），
  :approve 继续；:reject <反馈> 把反馈注回上游节点重跑后再审查。

runner 注册表 EXECUTOR_TO_RUNNER 可被 main 注入 SDK / app-server / mock 后端。
loop 的 exit_condition 判定由 judge（默认 LLM）完成；judge 可被外部注入以便测试。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import console
from .approvals import ApprovalBroker
from .claude_runner import ClaudeRunner
from .codex_app_server_runner import CodexAppServerRunner
from .codex_runner import CodexRunner
from .config import Config
from .llm import chat
from .models import CompiledGraph, Event, SubTask, TaskRun
from .planner import _extract_json

EXECUTOR_TO_RUNNER = {"claude": ClaudeRunner, "codex": CodexRunner, "codex-app-server": CodexAppServerRunner}

DEFAULT_MAX_LOOP_ITERATIONS = 5

JUDGE_PROMPT = """\
你是一个条件判定器。给你一个「退出条件」（自然语言）和当前任务的执行上下文，
请判断该退出条件当前是否已经满足。

严格只输出 JSON（不要 markdown 代码块）：
{"satisfied": true, "feedback": "……"}

- satisfied=true 表示条件已满足，可以退出循环继续往后。
- satisfied=false 时 feedback 写清「哪里不满足、需要怎么改」，该反馈会注回上游节点重跑。
"""


def llm_judge_condition(cfg: Config, condition: str, context: str) -> dict:
    """默认 loop 退出条件判定：调 LLM 返回 {satisfied, feedback}。失败按未满足处理。"""
    try:
        raw = chat(
            cfg.llm,
            [
                {"role": "system", "content": JUDGE_PROMPT},
                {"role": "user", "content": f"退出条件：\n{condition}\n\n当前上下文：\n{context[:8000]}"},
            ],
            temperature=0.0,
        )
        data = _extract_json(raw)
        return {"satisfied": bool(data.get("satisfied", False)), "feedback": str(data.get("feedback", ""))}
    except Exception as e:  # noqa: BLE001
        console.warn(f"loop 退出条件判定 LLM 失败（{e}），按未满足处理")
        return {"satisfied": False, "feedback": f"（条件判定不可用：{e}）"}


def _topo_layers(nodes: list[SubTask], edges) -> list[list[SubTask]]:
    """按边做拓扑分层（容错：遇环强制推进，不会死循环）。"""
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
        if not ready:  # 环：强制选最小 id 推进
            ready = [sorted(remaining)[0]]
        layers.append([by_id[nid] for nid in ready])
        for nid in ready:
            remaining.pop(nid, None)
            for nxt in adj[nid]:
                remaining[nxt] = remaining.get(nxt, 0) - 1
    return layers


class GraphExecutor:
    """执行 CompiledGraph。emit(run, event) 用于把事件透出给 REPL/会话层。"""

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
        judge=None,
        max_loop_iterations: int = DEFAULT_MAX_LOOP_ITERATIONS,
    ):
        self.cfg = cfg
        self.graph = graph
        self.broker = broker
        self.workdir = Path(workdir)
        self.emit = emit or self._default_emit
        self.goal = goal
        self.state = state or {}
        self.judge = judge or llm_judge_condition
        self.max_loop_iterations = max_loop_iterations

        self.runs: dict[str, TaskRun] = {}
        self._quit = threading.Event()
        self._active: dict[str, tuple] = {}
        self._active_lock = threading.Lock()

    # ---------- 事件 ----------
    @staticmethod
    def _default_emit(run: TaskRun | None, event: Event) -> None:
        console.event_line(event.summary(), source=event.source)

    def _emit(self, run: TaskRun | None, event: Event) -> None:
        self.emit(run, event)

    # ---------- 主入口 ----------
    def execute(self) -> list[TaskRun]:
        if not self.graph.nodes:
            return []
        try:
            if self.graph.loop:
                self._execute_with_loop()
            else:
                self._execute_dag()
        finally:
            self._quit.set()
        return list(self.runs.values())

    def stop(self) -> None:
        self._quit.set()
        with self._active_lock:
            runners = list(self._active.values())
        for runner, _ in runners:
            runner.stop()

    def send_message(self, target: str, text: str) -> list[str]:
        """REPL 注入：把消息路由给匹配的活跃 runner（@all/@claude/@codex/@<id>）。"""
        if not text:
            return []
        matched: list[str] = []
        with self._active_lock:
            items = list(self._active.items())
        for nid, (runner, _) in items:
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

    # ================================================================
    #  DAG 执行（含 human 审查节点）
    # ================================================================
    def _execute_dag(self) -> None:
        layers = _topo_layers(self.graph.nodes, self.graph.edges)
        for layer in layers:
            if self._quit.is_set():
                break
            self._run_layer(layer)

    def _run_layer(self, layer: list[SubTask]) -> None:
        human_nodes = [n for n in layer if n.executor == "human"]
        code_nodes = [n for n in layer if n.executor != "human"]
        if code_nodes:
            self._run_code_nodes_parallel(code_nodes)
        for node in human_nodes:
            self._run_human_with_rerun(node)

    def _run_code_nodes_parallel(self, nodes: list[SubTask]) -> None:
        threads = [
            threading.Thread(target=self._run_code_node, args=(node, ""), daemon=True, name=f"node-{node.id}")
            for node in nodes
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

    # ================================================================
    #  loop 执行
    # ================================================================
    def _execute_with_loop(self) -> None:
        graph = self.graph
        # 模板编译出的图天然线性（nodes 已是 t1..tn 顺序），loop 只加回边
        order = list(graph.nodes)
        back = graph.loop_back_edges[0] if graph.loop_back_edges else None
        loop_target = back.dst if back else (graph.entry or order[0].id)
        loop_source = back.src if back else (graph.entry or order[-1].id)

        idx_target = next((i for i, n in enumerate(order) if n.id == loop_target), 0)
        idx_source = next((i for i, n in enumerate(order) if n.id == loop_source), len(order) - 1)
        entry_nodes = order[:idx_target]
        loop_body = order[idx_target : idx_source + 1]
        exit_nodes = order[idx_source + 1 :]

        # 1) entry 部分（loop 之前的节点）
        for node in entry_nodes:
            if self._quit.is_set():
                return
            self._run_node_single(node)

        # 2) loop 迭代
        feedback = ""
        satisfied = False
        for it in range(self.max_loop_iterations):
            if self._quit.is_set():
                break
            for node in loop_body:
                ctx = ""
                if it > 0 and node.id == loop_target and feedback:
                    ctx = f"[上一轮未通过] 反馈：{feedback}\n请据此修正后重新执行。"
                self._run_node_single(node, extra_context=ctx)
            verdict = self.judge(self.cfg, graph.exit_condition, self._context_snapshot())
            satisfied = bool(verdict.get("satisfied", False))
            feedback = str(verdict.get("feedback", ""))
            console.status_line(
                "✓" if satisfied else "↻",
                f"loop 第 {it + 1}/{self.max_loop_iterations} 轮：退出条件{'满足' if satisfied else '未满足'} — {feedback[:120]}",
                "green" if satisfied else "yellow",
            )
            if satisfied:
                break
        if not satisfied:
            console.warn(f"loop 达 {self.max_loop_iterations} 轮仍未满足退出条件，交由外层决策")

        # 3) loop 之后的节点
        for node in exit_nodes:
            if self._quit.is_set():
                return
            self._run_node_single(node)

    def _context_snapshot(self) -> str:
        parts: list[str] = []
        if self.goal:
            parts.append(f"总体目标：{self.goal}")
        for nid, run in self.runs.items():
            if run.output:
                parts.append(f"[{nid}] {run.output[:2000]}")
        if self.state:
            parts.append("累计状态：" + json.dumps(self.state, ensure_ascii=False)[:2000])
        return "\n".join(parts)

    # ================================================================
    #  单节点分派
    # ================================================================
    def _run_node_single(self, node: SubTask, extra_context: str = "") -> None:
        if node.executor == "human":
            self._run_human_with_rerun(node)
        else:
            self._run_code_node(node, extra_context)

    # ================================================================
    #  code 节点执行
    # ================================================================
    def _run_code_node(self, node: SubTask, extra_context: str = "") -> TaskRun:
        workdir = str(self.workdir)
        run = TaskRun(task=node, workdir=workdir)
        self.runs[node.id] = run

        runner_cls = EXECUTOR_TO_RUNNER.get(node.executor)
        if runner_cls is None:
            run.status = "failed"
            run.error = f"未知 executor: {node.executor}"
            self._emit(run, Event(kind="error", source=node.executor, text=run.error))
            return run

        prompt = self._prompt_for(node, extra_context)
        runner = runner_cls(self.cfg, run, workdir, self._emit, prompt, broker=self.broker)
        with self._active_lock:
            self._active[node.id] = (runner, threading.current_thread())
        console.status_line("▶", f"启动 {node.id} [{node.executor}] {node.title}", "blue")
        # 展示发给 code agent 的任务信息（minimal 下也可见，让用户知道 agent 收到了什么）
        if node.description:
            console.dim(f"   任务: {node.description.strip().splitlines()[0][:120]}")
        if node.acceptance:
            console.dim(f"   验收: {node.acceptance.strip().splitlines()[0][:120]}")

        try:
            runner.start()
            deadline = time.time() + self.cfg.timeout_per_task
            while not runner.is_done() and not self._quit.is_set():
                # runner 等待审批时不触发超时（用户可能正在决策）
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

    # ================================================================
    #  human 审查节点（阻塞 + 驳回重跑上游）
    # ================================================================
    def _run_human_with_rerun(self, node: SubTask) -> None:
        while not self._quit.is_set():
            decision = self._run_human_node(node)
            if decision["approved"]:
                return
            feedback = decision["feedback"] or "需要修改"
            preds = self._predecessors(node.id)
            if not preds:
                console.warn(f"审查点 {node.id} 被驳回，但没有可重跑的上游节点，跳过重跑")
            for pid in preds:
                pred = self.graph.node_by_id(pid)
                if pred is not None and pred.executor != "human":
                    console.status_line("↩", f"审查驳回，重跑上游 {pred.id} [{pred.executor}]", "yellow")
                    self._run_code_node(pred, extra_context=f"[审查驳回反馈] {feedback}\n请据此修改后再执行。")
            # 循环回到审查节点

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
