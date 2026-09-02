
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from . import console
from .approvals import ApprovalBroker
from .config import Config
from .models import CompiledGraph, Event, SubTask, TaskRun, task_run_from_dict, validate_graph
from .policy_hooks import HookChain
from .runner_base import RunnerBase
from .runner_factory import create_runner
from .tool_catalog import ToolCatalog
from .workflow import stage_for_task


logger = logging.getLogger(__name__)

_NON_FAILOVER_FAILURES = frozenset(("permission_denied", "user_stopped", "invalid_task"))
_SAME_EXECUTOR_RETRY_FAILURES = frozenset(("transient", "timeout", "protocol_error"))


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
        repository_dir: str | Path | None = None,
        emit=None,
        goal: str = "",
        state: dict | None = None,
        resume_runs: dict[str, dict] | None = None,
        on_task_complete=None,
        tool_catalog: ToolCatalog | None = None,
        hook_chain: HookChain | None = None,
    ):
        self.cfg = cfg
        self.graph = graph
        self.broker = broker
        self.workdir = Path(workdir)
        self.emit = emit or self._default_emit
        self.goal = goal
        self.state = state if state is not None else {}
        self.resume_runs = resume_runs or {}
        self.on_task_complete = on_task_complete
        self.tool_catalog = tool_catalog or ToolCatalog.from_config(cfg)
        self.hook_chain = hook_chain or HookChain.from_config(cfg)
        validate_graph(graph)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.repository_dir = Path(repository_dir).expanduser().resolve() if repository_dir else None
        if any(node.workdir_scope == "repository" for node in graph.nodes):
            if self.repository_dir is None:
                raise ValueError("任务需要代码仓库目录，但未提供 repository_dir")
            if not self.repository_dir.is_dir():
                raise ValueError(f"代码仓库目录不存在或不是目录: {self.repository_dir}")

        self.runs: dict[str, TaskRun] = {}
        self._runs_lock = threading.Lock()
        self._attempt_context: dict[str, tuple[str, str]] = {}
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

    def status_snapshot(self) -> list[dict]:
        """返回当前任务图的轻量状态快照，供 TUI 的 :status 使用。

        ``runs`` 只会在任务真正开始后创建记录，因此这里把尚未启动的节点
        明确补成 ``pending``，避免用户把“没有记录”误解成任务丢失。快照不
        暴露 runner 或协议对象，只包含适合终端展示的稳定字段。
        """
        with self._runs_lock:
            runs = dict(self.runs)
        with self._active_lock:
            active_ids = set(self._active)

        now = time.time()
        rows: list[dict] = []
        for node in self.graph.nodes:
            run = runs.get(node.id)
            if run is None:
                status = "pending"
                duration = 0.0
                output = ""
                error = ""
                attempts = 0
                injections = 0
            else:
                status = run.status
                end = run.ended_at or now
                duration = max(0.0, end - run.started_at) if run.started_at else 0.0
                output = run.output
                error = run.error
                attempts = len(run.attempts) or (1 if run.started_at else 0)
                injections = sum(1 for event in run.events if event.kind == "user_message")
            rows.append(
                {
                    "id": node.id,
                    "title": node.title,
                    "executor": node.executor,
                    "status": status,
                    "active": node.id in active_ids,
                    "duration": duration,
                    "attempts": attempts,
                    "injections": injections,
                    "output": output,
                    "error": error,
                    "depends_on": list(node.depends_on),
                }
            )
        return rows

    @property
    def pending_approval_ids(self) -> list[str]:
        return self.broker.pending_ids

    def _execute_dag(self) -> None:
        self._restore_successful_runs()
        layers = _topo_layers(self.graph.nodes, self.graph.edges)
        started_stages: set[str] = set()
        for layer in layers:
            if self._quit.is_set():
                break
            for node in layer:
                stage = stage_for_task(self.graph.workflow, node.id)
                if stage is not None and stage["id"] not in started_stages:
                    started_stages.add(stage["id"])
                    self._emit(
                        None,
                        Event(
                            kind="interaction",
                            source="orchestrator",
                            text=f"开始阶段 {stage.get('name', stage['id'])}",
                            data={"workflow_stage": "start", "stage_id": stage["id"], "task_ids": stage.get("task_ids", [])},
                        ),
                    )
            self._run_layer(layer)
            for stage in self.graph.workflow:
                task_ids = stage.get("task_ids", [])
                if task_ids and all(self.runs.get(task_id) and self.runs[task_id].status == "success" for task_id in task_ids):
                    marker = f"completed:{stage['id']}"
                    if marker not in started_stages:
                        started_stages.add(marker)
                        self._emit(
                            None,
                            Event(
                                kind="interaction",
                                source="orchestrator",
                                text=f"完成阶段 {stage.get('name', stage['id'])}",
                                data={"workflow_stage": "complete", "stage_id": stage["id"], "task_ids": task_ids},
                            ),
                        )

    def _run_layer(self, layer: list[SubTask]) -> None:
        runnable: list[SubTask] = []
        for node in layer:
            restored = self.runs.get(node.id)
            if restored is not None and restored.status == "success":
                console.status_line("↪", f"恢复 {node.id} [{node.executor}]：已成功，跳过执行", "dim")
                continue
            blocked = self._blocked_predecessors(node.id)
            if blocked:
                self._skip_node(node, blocked)
            else:
                runnable.append(node)

        human_nodes = [n for n in runnable if n.executor == "human"]
        code_nodes = [n for n in runnable if n.executor != "human"]
        if code_nodes:
            # 共享工作区中只允许显式标为 read_only 的任务并发；
            # 只要同层存在写任务，就整层串行，避免读写竞态。
            if all(node.workspace_access == "read_only" for node in code_nodes):
                self._run_code_nodes_parallel(code_nodes)
            else:
                self._run_code_nodes_serial(code_nodes)
        for node in human_nodes:
            self._run_human_with_rerun(node)

    def _run_code_nodes_parallel(self, nodes: list[SubTask]) -> None:
        try:
            max_workers = max(1, int(getattr(self.cfg, "max_parallel", 1)))
        except (TypeError, ValueError):
            max_workers = 1
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="node") as pool:
            futures = [pool.submit(self._run_code_node_with_failover, node, "") for node in nodes]
            for future in futures:
                future.result()

    def _run_code_nodes_serial(self, nodes: list[SubTask]) -> None:
        for node in nodes:
            self._run_code_node_with_failover(node, "")

    def _failover_executors(self, executor: str) -> list[str]:
        """返回当前任务允许使用的执行器顺序，最多在另一种代码 agent 上重试。"""
        if executor not in {"claude", "codex"}:
            return [executor]
        dispatch = getattr(self.cfg, "dispatch", None)
        if getattr(dispatch, "failover_enabled", True) is not True:
            return [executor]
        try:
            max_attempts = int(getattr(dispatch, "max_failover_attempts", 1))
        except (TypeError, ValueError):
            max_attempts = 0
        if max_attempts <= 0:
            return [executor]
        alternate = "codex" if executor == "claude" else "claude"
        return [executor, alternate]

    @staticmethod
    def _attempt_summary(
        run: TaskRun,
        *,
        attempt_id: str = "",
        parent_attempt_id: str = "",
        failure_class: str = "",
        retryable: bool = False,
        attempt_no: int = 0,
        retry_no: int = 0,
        transition: str = "final",
        delay: float = 0.0,
    ) -> dict:
        return {
            "attempt_id": attempt_id,
            "parent_attempt_id": parent_attempt_id,
            "attempt_no": attempt_no,
            "retry_no": retry_no,
            "executor": run.task.executor,
            "status": run.status,
            "exit_code": run.exit_code,
            "error": run.error,
            "failure_class": failure_class,
            "retryable": retryable,
            "transition": transition,
            "delay": delay,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "cost_usd": run.cost_usd,
        }

    @staticmethod
    def _can_failover(run: TaskRun, failure_class: str = "") -> bool:
        """主动停止或权限拒绝不能通过切换 agent 绕过。"""
        if run.status != "failed":
            return False
        if failure_class in _NON_FAILOVER_FAILURES:
            return False
        return not any(
            event.kind == "permission_result"
            and isinstance(event.data, dict)
            and event.data.get("allowed") is False
            for event in run.events
        )

    @staticmethod
    def _classify_failure(run: TaskRun, *, stopped: bool = False) -> str:
        """把后端结果归一成可用于重试决策的失败类别。"""
        if run.status != "failed":
            return ""
        if any(
            event.kind == "permission_result"
            and isinstance(event.data, dict)
            and event.data.get("allowed") is False
            for event in run.events
        ):
            return "permission_denied"
        text = f"{run.error} {run.output}".casefold()
        if stopped or any(word in text for word in ("用户停止", "主动停止", "user stopped", "cancelled", "canceled")):
            return "user_stopped"
        if any(word in text for word in ("超时", "timeout", "timed out")):
            return "timeout"
        if any(word in text for word in ("找不到可执行文件", "未安装", "not found", "no such file", "executable")):
            return "missing_binary"
        if any(word in text for word in ("协议", "protocol", "json", "解析事件", "malformed")):
            return "protocol_error"
        if any(word in text for word in ("连接", "网络", "connection", "network", "transport", "temporarily unavailable")):
            return "transient"
        return "execution_error"

    @staticmethod
    def _retry_delay(cfg: Config, retry_no: int) -> float:
        retry = getattr(cfg, "retry", None)
        initial = max(0.001, float(getattr(retry, "initial_delay", 1.0)))
        maximum = max(initial, float(getattr(retry, "max_delay", 30.0)))
        # 退避达到上限后不再继续放大指数，避免异常配置导致超大整数计算。
        exponent = min(max(0, retry_no - 1), 20)
        return min(maximum, initial * (2 ** exponent))

    def _run_code_node_with_failover(self, node: SubTask, extra_context: str = "") -> TaskRun:
        executors = self._failover_executors(node.executor)
        attempts: list[dict] = []
        final_run: TaskRun | None = None

        attempt_no = 0
        for executor_index, executor in enumerate(executors):
            retry_no = 0
            advance_executor = False
            while True:
                if self._quit.is_set():
                    break
                attempt_no += 1
                attempt_node = node if executor == node.executor else replace(node, executor=executor)
                attempt_id = f"{node.id}-a{attempt_no}-{secrets.token_hex(3)}"
                parent_attempt_id = attempts[-1].get("attempt_id", "") if attempts else ""
                context = extra_context
                transition = "initial" if not attempts else "retry"
                if attempts:
                    previous = attempts[-1]
                    if executor != previous["executor"]:
                        transition = "failover"
                        transition_context = (
                            f"[故障转移尝试 {attempt_no}/{len(executors)}] "
                            f"前一个 agent（{previous['executor']}）执行失败：{previous['error'] or '未知错误'}\n"
                            "请继承当前工作目录中已经产生的有效产物，先检查失败原因，再继续完成原任务；"
                            "不要修改任务目标、描述或验收标准。"
                        )
                        console.warn(
                            f"任务 {node.id} 的 {previous['executor']} 执行失败，切换到 {executor} 尝试"
                        )
                    else:
                        transition_context = (
                            f"[同一 executor 第 {retry_no + 1} 次重试] 上一次执行失败："
                            f"{previous['error'] or '未知错误'}；请先检查当前工作目录中的部分产物。"
                        )
                    context = f"{context}\n\n{transition_context}" if context else transition_context

                with self._runs_lock:
                    self._attempt_context[node.id] = (attempt_id, parent_attempt_id)
                final_run = self._run_code_node(attempt_node, context)
                with self._runs_lock:
                    self._attempt_context.pop(node.id, None)
                failure_class = self._classify_failure(final_run, stopped=self._quit.is_set())
                retryable = failure_class in _SAME_EXECUTOR_RETRY_FAILURES
                final_run.attempt_id = attempt_id
                final_run.parent_attempt_id = parent_attempt_id
                final_run.failure_class = failure_class
                final_run.retryable = retryable

                retry_cfg = getattr(self.cfg, "retry", None)
                max_retries = max(0, int(getattr(retry_cfg, "max_retries", 1)))
                can_retry_same = (
                    final_run.status == "failed"
                    and retryable
                    and retry_no < max_retries
                    and not self._quit.is_set()
                )
                delay = self._retry_delay(self.cfg, retry_no + 1) if can_retry_same else 0.0
                if can_retry_same:
                    transition = "retry"
                elif final_run.status == "success":
                    transition = "success"
                elif self._can_failover(final_run, failure_class) and executor_index + 1 < len(executors):
                    transition = "failover"
                else:
                    transition = "final"

                attempts.append(
                    self._attempt_summary(
                        final_run,
                        attempt_id=attempt_id,
                        parent_attempt_id=parent_attempt_id,
                        failure_class=failure_class,
                        retryable=retryable,
                        attempt_no=attempt_no,
                        retry_no=retry_no,
                        transition=transition,
                        delay=delay,
                    )
                )

                if can_retry_same:
                    self._emit(
                        final_run,
                        Event(
                            kind="retry",
                            source="orchestrator",
                            text=f"任务 {node.id} 在 {executor} 上第 {retry_no + 1} 次重试，{delay:.1f}s 后继续",
                            data={
                                "task_id": node.id,
                                "attempt_id": attempt_id,
                                "failure_class": failure_class,
                                "retry_no": retry_no + 1,
                                "delay": delay,
                                "transition": "retry",
                            },
                        ),
                    )
                    time.sleep(delay)
                    retry_no += 1
                    continue

                if not self._can_failover(final_run, failure_class) or executor_index + 1 >= len(executors):
                    break
                self._emit(
                    final_run,
                    Event(
                        kind="retry",
                        source="orchestrator",
                        text=f"任务 {node.id} 故障转移：{executor} → {executors[executor_index + 1]}",
                        data={
                            "failover": True,
                            "task_id": node.id,
                            "attempt_id": attempt_id,
                            "from_executor": executor,
                            "to_executor": executors[executor_index + 1],
                            "failure_class": failure_class,
                            "transition": "failover",
                        },
                    ),
                )
                advance_executor = True
                break
            if not advance_executor:
                break

        if final_run is None:
            final_run = TaskRun(
                task=node,
                status="failed",
                exit_code=1,
                error="编排器已停止，未启动任务",
                workdir=str(self._workdir_for(node)),
            )
            attempt_id = f"{node.id}-a1-{secrets.token_hex(3)}"
            final_run.attempt_id = attempt_id
            final_run.failure_class = "user_stopped"
            attempts.append(
                self._attempt_summary(
                    final_run,
                    attempt_id=attempt_id,
                    failure_class="user_stopped",
                    attempt_no=1,
                )
            )

        final_run.attempts = attempts
        # 单次尝试已经完成过回调；这里再次保存最终的 attempts 摘要，保证恢复/审计信息完整。
        self._record_run(final_run)
        return final_run

    def _restore_successful_runs(self) -> None:
        """将与当前计划匹配的成功快照恢复到 runs，供下游任务读取输出。"""
        for node in self.graph.nodes:
            saved = self.resume_runs.get(node.id)
            if not isinstance(saved, dict) or saved.get("status") != "success":
                continue
            try:
                run = task_run_from_dict(saved, task=node)
            except (TypeError, ValueError) as exc:
                logger.warning("恢复任务 %s 快照失败，将重新执行: %s", node.id, exc)
                continue
            if run.status == "success":
                with self._runs_lock:
                    self.runs[node.id] = run

    def _record_run(self, run: TaskRun) -> None:
        if self.on_task_complete is None:
            return
        try:
            self.on_task_complete(run)
        except Exception:
            logger.exception("任务 %s 完成回调失败", run.task.id)

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
        with self._runs_lock:
            self.runs[node.id] = run
        self._record_run(run)
        console.status_line("↷", f"跳过 {node.id} [{node.executor}]：{reason}", "yellow")
        self._emit(run, Event(kind="error", source="orchestrator", text=reason, data={"skipped": True}))

    def _run_code_node(self, node: SubTask, extra_context: str = "") -> TaskRun:
        workdir = str(self._workdir_for(node))
        with self._runs_lock:
            attempt_id, parent_attempt_id = self._attempt_context.pop(node.id, ("", ""))
            run = TaskRun(
                task=node,
                workdir=workdir,
                attempt_id=attempt_id,
                parent_attempt_id=parent_attempt_id,
            )
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
                tool_catalog=self.tool_catalog,
                hook_chain=self.hook_chain,
            )
        except ValueError as exc:
            run.status = "failed"
            run.error = str(exc)
            self._emit(run, Event(kind="error", source=node.executor, text=run.error))
            self._record_run(run)
            return run

        if node.internal_loop is not None:
            runner.set_internal_loop(node.internal_loop)
        with self._active_lock:
            self._active[node.id] = runner
        console.status_line("▶", f"启动 {node.id} [{node.executor}] {node.title}", "blue")
        console.dim(f"   工作目录: {workdir}")
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
            self._record_run(run)
            if run.status == "success":
                console.status_line("✓", f"{node.id} [{node.executor}] 完成（{run.duration:.1f}s, ${run.cost_usd:.4f}）", "green")
            else:
                console.status_line("✗", f"{node.id} [{node.executor}] {run.status}: {run.error or '见上方输出'}", "red")
        return run

    def _prompt_for(self, node: SubTask, extra_context: str = "") -> str:
        parts: list[str] = []
        runtime = getattr(self.cfg, "runtime", None)
        max_context = max(200, int(getattr(runtime, "max_context_chars", 16000)))
        max_dependency = max(200, int(getattr(runtime, "max_dependency_chars", 8000)))
        max_state = max(100, int(getattr(runtime, "max_state_chars", 2000)))
        if self.goal:
            parts.append(f"总体目标：{self.goal[:max_context]}")
        parts.append(f"任务 {node.id}: {node.title[:max_context]}")
        parts.append(node.description[:max_context])
        parts.append(
            "任务标题、任务描述和完成标准是模板或上游计划提供的只读约束；"
            "如果发现约束之间存在矛盾，请报告矛盾，不要自行改写目标或验收标准。"
        )
        task_workdir = self._workdir_for(node)
        parts.append(
            f"当前任务工作目录：{task_workdir}\n"
            f"中间产物目录：{self.workdir}\n"
            "请先读取当前任务工作目录和中间产物目录中前置 agent 已产生的代码、文件和日志，再继续本任务；"
            "不要重复从零分析整个模板。"
        )
        tool_prompt = (
            "工具/技能可以由你自主发现和选择：请先检查当前执行器实际可调用的工具、命令、技能或 MCP 能力，"
            "再选择最合适的调用方式。模板中的工具提示不是硬约束；若提示工具不可用，优先寻找等价的可用工具或命令，"
            "只有确实没有可行替代方案时才报告缺口。"
        )
        tool_query = node.tool.strip() or ("读取 检查" if node.workspace_access == "read_only" else "文件 命令")
        discovered_tools = self.tool_catalog.describe_for(
            tool_query[:160],
            executor=node.executor,
            workspace_access=node.workspace_access,
            workdir_scope=node.workdir_scope,
        )
        hint_note = ""
        if node.tool:
            hint_decision = self.tool_catalog.decision(
                node.tool,
                executor=node.executor,
                workspace_access=node.workspace_access,
                workdir_scope=node.workdir_scope,
            )
            if hint_decision.descriptor is not None and not hint_decision.allowed:
                hint_note = f"\n模板工具提示未通过当前策略：{hint_decision.reason}"
        parts.append(
            f"当前 executor={node.executor} 可发现的工具（按提示检索，工具发现不等于绕过权限）：\n{discovered_tools}{hint_note}"
        )
        if node.tool:
            parts.append(f"工具/技能提示（可替换）：{node.tool}\n{tool_prompt}")
        else:
            parts.append(tool_prompt)
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
            dependency_outputs = [self.runs[d].output[:max_dependency] for d in deps]
            parts.append("\n依赖任务输出（作为上下文）：\n" + "\n---\n".join(dependency_outputs)[:max_dependency])
        dep_ids = [d for d in deps if self.runs[d].status == "success"]
        if dep_ids:
            parts.append(
                "\n注意：前置任务 (" + ", ".join(dep_ids) + ") 的文件产物已落在当前工作目录中，请直接读取使用。"
            )
        if self.state:
            parts.append("\n累计状态：" + json.dumps(self.state, ensure_ascii=False)[:max_state])
        if extra_context:
            parts.append(extra_context[:max_context])
        prompt = "\n\n".join(parts)
        if len(prompt) <= max_context:
            return prompt
        tail_size = max(80, max_context // 3)
        head_size = max_context - tail_size - 40
        return prompt[:head_size] + "\n...[上下文已按运行时预算截断]...\n" + prompt[-tail_size:]

    def _workdir_for(self, node: SubTask) -> Path:
        """按任务类型选择真实操作目录；session workspace 不作为代码仓库。"""
        if node.workdir_scope == "repository":
            if self.repository_dir is None:
                raise ValueError(f"任务 {node.id} 需要代码仓库目录，但当前未配置")
            return self.repository_dir
        return self.workdir

    def _predecessors(self, node_id: str) -> list[str]:
        return [e.src for e in self.graph.edges if e.dst == node_id]

    def _run_human_with_rerun(self, node: SubTask) -> TaskRun:
        while not self._quit.is_set():
            decision = self._run_human_node(node)
            if decision["approved"]:
                now = time.time()
                run = TaskRun(
                    task=node,
                    status="success",
                    exit_code=0,
                    started_at=now,
                    ended_at=now,
                    workdir=str(self.workdir),
                )
                with self._runs_lock:
                    self.runs[node.id] = run
                self._record_run(run)
                return run
            feedback = decision["feedback"] or "需要修改"
            preds = self._predecessors(node.id)
            if not preds:
                console.warn(f"审查点 {node.id} 被驳回，但没有可重跑的上游节点，跳过重跑")
                return self._human_failure(node, "审查驳回且没有可重跑的上游节点")
            rerun = False
            for pid in preds:
                pred = self.graph.node_by_id(pid)
                if pred is not None and pred.executor != "human":
                    rerun = True
                    console.status_line("↩", f"审查驳回，重跑上游 {pred.id} [{pred.executor}]", "yellow")
                    self._run_code_node_with_failover(
                        pred,
                        extra_context=f"[审查驳回反馈] {feedback}\n请据此修改后再执行。",
                    )
            if not rerun:
                console.warn(f"审查点 {node.id} 被驳回，但没有可重跑的代码上游节点，跳过重跑")
                return self._human_failure(node, "审查驳回且没有可重跑的代码上游节点")

        return self._human_failure(node, "编排器已停止")

    def _human_failure(self, node: SubTask, reason: str) -> TaskRun:
        run = TaskRun(task=node, status="failed", exit_code=1, error=reason, workdir=str(self.workdir))
        with self._runs_lock:
            self.runs[node.id] = run
        self._record_run(run)
        return run

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
