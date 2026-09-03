
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from . import console
from .config import Config
from .graph_executor import GraphExecutor
from .llm import LLMError
from .models import CompiledGraph, Event, GraphEdge, Plan, Session, SubTask, TaskRun, task_run_to_dict
from .planner import _extract_json, _validate, plan_with_llm, plan_with_single_code_agent
from .session import SessionStore, graph_signature
from .template_compiler import compile_template, load_named_template, validate_template_contract
from .workflow import apply_workflow_barriers


logger = logging.getLogger(__name__)

EVALUATOR_INSTRUCTION = """\
请先读取当前工作目录中的产物核实，再严格只输出 JSON（不要 markdown 代码块）：
{"achieved": true, "feedback": "……"}
- achieved=true 表示总体目标已达成；否则 false，feedback 写清还差什么、下一步要做什么。
"""

REVIEWER_INSTRUCTION = """\
你是独立代码审查员。请先读取当前任务工作目录中的实际文件和测试结果，
不要只相信执行结果摘要，也不要修改任何文件。你必须严格只输出 JSON（不要 markdown 代码块）：
{
  "approved": true,
  "confidence": 0,
  "feedback": "总体结论",
  "findings": [
    {"title":"问题标题", "severity":"critical|high|medium|low", "confidence":0,
     "evidence":"文件路径、测试输出或可复现事实", "recommendation":"建议"}
  ]
}
只有有明确证据的问题才可以作为阻断意见；不要报告纯风格偏好或无法验证的猜测。
"""

_REVIEW_ROLES = (
    ("correctness", "重点检查功能正确性、任务验收标准、边界条件、回归和测试证据。"),
    ("safety", "重点检查权限、工作目录、数据破坏、错误处理、信息泄露和失败转移是否安全。"),
    ("maintainability", "重点检查项目约定、接口兼容性、重复逻辑、可测试性和后续维护风险。"),
)


def plan_to_graph(plan: Plan, template: dict | None, cfg: Config | None = None) -> CompiledGraph:
    _validate(plan)
    nodes = plan.tasks
    if not nodes:
        return CompiledGraph()

    has_deps = any(t.depends_on for t in nodes)
    if has_deps:
        edges = [GraphEdge(src=d, dst=t.id) for t in nodes for d in t.depends_on if d]
    else:
        edges = [GraphEdge(src=nodes[i].id, dst=nodes[i + 1].id) for i in range(len(nodes) - 1)]

    workflow = []
    if isinstance(plan.orchestration, dict):
        raw_workflow = plan.orchestration.get("workflow") or []
        if isinstance(raw_workflow, list):
            workflow = [dict(stage) for stage in raw_workflow if isinstance(stage, dict)]
    graph = CompiledGraph(nodes=nodes, edges=edges, entry=nodes[0].id, workflow=workflow)

    if template and template.get("suggested_tasks"):
        validate_template_contract(graph, template)

    if template and cfg is not None:
        loop_info = (plan.orchestration or {}).get("loop") if hasattr(plan, "orchestration") else None
        tpl_graph = compile_template(cfg, template, loop_info=loop_info)
        template_nodes = {node.id: node for node in tpl_graph.nodes}
        for node in graph.nodes:
            compiled = template_nodes.get(node.id)
            if compiled is not None and node.internal_loop is None:
                node.internal_loop = compiled.internal_loop
    apply_workflow_barriers(graph, workflow)
    return graph


def _parse_verdict(output: str) -> dict:
    try:
        data = _extract_json(output)
        return {"achieved": bool(data.get("achieved", False)), "feedback": str(data.get("feedback", ""))}
    except Exception:
        logger.warning("evaluator 返回内容无法解析", exc_info=True)
        return {"achieved": False, "feedback": f"（evaluator 未返回有效判定，原文：{output[:200]}）"}


def _parse_review(output: str, role: str) -> dict:
    """解析 reviewer 的结构化结果；不接受无法给出证据的隐式通过。"""
    try:
        data = _extract_json(output)
        raw_findings = data.get("findings", [])
        if not isinstance(raw_findings, list):
            raise ValueError("findings 必须是数组")
        findings = []
        for item in raw_findings:
            if not isinstance(item, dict):
                continue
            try:
                confidence = max(0, min(100, int(item.get("confidence", 0))))
            except (TypeError, ValueError):
                confidence = 0
            findings.append(
                {
                    "title": str(item.get("title", "") or "")[:240],
                    "severity": str(item.get("severity", "medium") or "medium").lower(),
                    "confidence": confidence,
                    "evidence": str(item.get("evidence", "") or "")[:1200],
                    "recommendation": str(item.get("recommendation", "") or "")[:1000],
                }
            )
        try:
            confidence = max(0, min(100, int(data.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0
        return {
            "valid": True,
            "role": role,
            "approved": bool(data.get("approved", False)),
            "confidence": confidence,
            "feedback": str(data.get("feedback", "") or "")[:1500],
            "findings": findings,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("reviewer %s 返回内容无法解析: %s", role, exc)
        return {
            "valid": False,
            "role": role,
            "approved": False,
            "confidence": 0,
            "feedback": f"reviewer {role} 未返回有效证据报告：{output[:200]}",
            "findings": [],
        }


class GoalLoop:

    def __init__(
        self,
        cfg: Config,
        broker,
        store: SessionStore,
        *,
        emit=None,
        planner=None,
        evaluator=None,
        template: dict | None = None,
        repository_dir: str | None = None,
    ):
        self.cfg = cfg
        self.broker = broker
        self.store = store
        self.emit = emit or (lambda run, event: console.event_line(event.summary(), source=event.source))
        self.planner = planner or self._default_planner
        self.evaluator = evaluator or self._code_agent_evaluator
        self.template = template
        self.repository_dir = str(Path(repository_dir or Path.cwd()).expanduser().resolve())
        self.session: Session | None = None
        self.current: GraphExecutor | None = None
        self._evaluation_workdir_scope = "session"
        self._stop_event = threading.Event()
        self._persist_lock = threading.RLock()

    def _emit(self, run, event) -> None:
        self.emit(run, event)
        if self.store and self.session:
            task_id = run.task.id if run is not None else str(event.data.get("node", "review"))
            self.store.append_event(
                self.session.session_id,
                task_id,
                {
                    "ts": event.ts,
                    "kind": event.kind,
                    "source": event.source,
                    "text": event.text,
                    "data": event.data,
                },
            )

    def run_goal(self, goal: str, session: Session) -> Session:
        self.session = session
        if not session.goal:
            session.goal = goal
        max_iter = max(1, int(self.cfg.goal_loop.max_iterations))
        completed_this_call = 0
        resume_graph = self._load_resume_graph(session)

        while completed_this_call < max_iter:
            if self._stop_event.is_set():
                break
            completed_this_call += 1
            resume_runs = None

            try:
                if resume_graph is not None:
                    graph = resume_graph
                    resume_graph = None
                    plan = Plan(objective=session.goal or goal, tasks=graph.nodes, rationale="恢复已保存任务图")
                    resume_runs = session.task_runs
                    console.banner(f"恢复 goal 迭代 {session.iteration}（本次第 {completed_this_call}/{max_iter} 轮）")
                else:
                    session.iteration += 1
                    session.task_runs.clear()
                    session.plan_signature = ""
                    console.banner(f"goal 迭代 {session.iteration}（本次第 {completed_this_call}/{max_iter} 轮）")
                    plan = self._plan(goal, session.state)
                    _validate(plan)
                    console.dim(f"拆分：{len(plan.tasks)} 个任务" + (f"（模板 {plan.template}）" if plan.template else ""))

                    template = load_named_template(plan.template) if plan.template else None
                    graph = plan_to_graph(plan, template, self.cfg)
                    session.status = "running"
                    self.store.save(session, graph)
                self._evaluation_workdir_scope = (
                    "repository"
                    if any(node.workdir_scope == "repository" for node in graph.nodes)
                    else "session"
                )
                session.status = "running"
                summary = self._execute(graph, session, resume_runs=resume_runs, persist_runs=True)
            except Exception as e:  # noqa: BLE001
                logger.exception("任务图执行失败")
                session.status = "failed"
                session.state["error"] = str(e)
                self.store.save(session)
                self._emit(None, Event(kind="error", source="orchestrator", text=f"任务图执行失败: {e}"))
                return session
            if self._stop_event.is_set():
                break

            review = self._run_review_stage(goal, summary, graph, session)
            if review is not None:
                session.state["last_review"] = review

            if review is not None and not review["approved"]:
                verdict = {
                    "achieved": False,
                    "feedback": "独立审查未通过：" + str(review.get("feedback") or "请根据审查报告修正并重试"),
                }
            else:
                try:
                    verdict = self.evaluator(goal, session.state, summary, session)
                except Exception as e:  # noqa: BLE001
                    logger.exception("goal evaluator 执行失败")
                    session.status = "failed"
                    session.state["error"] = str(e)
                    self.store.save(session, graph)
                    self._emit(None, Event(kind="error", source="orchestrator", text=f"目标评估失败: {e}"))
                    return session
            achieved = bool(verdict.get("achieved", False))
            feedback = str(verdict.get("feedback", ""))

            session.state["last_summary"] = summary
            session.history.append(
                {
                    "iteration": session.iteration,
                    "objective": plan.objective,
                    "template": plan.template,
                    "achieved": achieved,
                    "feedback": feedback,
                    "review": review,
                }
            )
            self.store.save(session, graph)

            if achieved:
                session.status = "goal_achieved"
                self.store.save(session, graph)
                console.ok(f"goal 达成（第 {session.iteration} 轮）")
                return session

            session.state["feedback"] = feedback
            console.status_line("↻", f"未达成，feedback 并入 state 进入下一轮：{feedback[:120]}", "yellow")

        if self._stop_event.is_set():
            session.status = "stopped"
            self.store.save(session)
            console.warn("已终止执行")
        else:
            session.status = "paused"
            self.store.save(session)
            console.warn(f"本次已执行 {max_iter} 轮仍未达成，交回 REPL 决策")
        return session

    def _run_review_stage(
        self,
        goal: str,
        summary: str,
        graph: CompiledGraph,
        session: Session,
    ) -> dict | None:
        """运行独立 reviewer，再让验证 reviewer 过滤误报并确认证据。"""
        cfg = getattr(self.cfg, "review", None)
        if cfg is None or not cfg.enabled or cfg.reviewer_count <= 0:
            return None

        reviewer_executor = self.cfg.dispatch.verification_executor
        if reviewer_executor not in ("claude", "codex"):
            reviewer_executor = "codex"
        review_scope = "repository" if any(
            node.workdir_scope == "repository" for node in graph.nodes
        ) else "session"
        count = min(max(1, int(cfg.reviewer_count)), max(1, len(_REVIEW_ROLES)))
        nodes: list[SubTask] = []
        for index in range(count):
            role, focus = _REVIEW_ROLES[index]
            nodes.append(
                SubTask(
                    id=f"review-{index + 1}",
                    title=f"独立审查：{role}",
                    description=(
                        f"总体目标：{goal}\n\n执行结果摘要：\n{summary[:6000]}\n\n"
                        f"当前审查角度：{focus}\n\n{REVIEWER_INSTRUCTION}"
                    ),
                    executor=reviewer_executor,
                    acceptance="输出包含 approved、confidence、feedback、findings 和 evidence 的 JSON",
                    workspace_access="read_only",
                    workdir_scope=review_scope,
                )
            )
        reviewer_graph = CompiledGraph(nodes=nodes, edges=[], entry=nodes[0].id)
        self._emit(
            None,
            Event(
                kind="interaction",
                source="orchestrator",
                text=f"启动 {len(nodes)} 个独立 reviewer（{reviewer_executor}）",
                data={"review_stage": "reviewers", "count": len(nodes)},
            ),
        )
        reviewer_runs = self._execute_graph(reviewer_graph, session, persist_runs=False)
        reports: list[dict] = []
        for node, run in zip(nodes, reviewer_runs):
            report = _parse_review(run.output, node.title) if run.status == "success" else {
                "valid": False,
                "role": node.title,
                "approved": False,
                "confidence": 0,
                "feedback": f"reviewer 执行失败：{run.error or '未知错误'}",
                "findings": [],
            }
            reports.append(report)
            self._emit(
                None,
                Event(
                    kind="review_result",
                    source=reviewer_executor,
                    text=str(report.get("feedback") or "")[:500],
                    data={
                        "review_stage": "reviewer",
                        "role": report.get("role"),
                        "valid": report.get("valid", False),
                        "approved": report.get("approved", False),
                        "confidence": report.get("confidence", 0),
                        "finding_count": len(report.get("findings") or []),
                    },
                ),
            )

        report_text = json.dumps(reports, ensure_ascii=False)[:9000]
        validator = SubTask(
            id="review-validator",
            title="审查证据验证",
            description=(
                f"总体目标：{goal}\n\n执行结果摘要：\n{summary[:4500]}\n\n"
                f"独立审查报告：\n{report_text}\n\n"
                "请读取当前工作目录核实报告中的文件、测试和行为证据，过滤误报。"
                f"只有置信度不低于 {cfg.min_confidence} 且能被实际文件或可复现测试支持的问题才应阻断；"
                "如果报告无有效证据，可以否决该报告，但不能凭空添加问题。\n"
                "严格只输出 JSON（不要 markdown 代码块）："
                '{"approved":true,"confidence":0,"feedback":"验证结论",'
                '"findings":[{"title":"已验证问题","severity":"high",'
                '"confidence":0,"evidence":"验证证据","recommendation":"建议"}]}'
            ),
            executor=reviewer_executor,
            acceptance="验证 reviewer 报告中的证据，并输出 approved JSON",
            workspace_access="read_only",
            workdir_scope=review_scope,
        )
        validator_graph = CompiledGraph(nodes=[validator], edges=[], entry=validator.id)
        self._emit(
            None,
            Event(
                kind="interaction",
                source="orchestrator",
                text="启动审查证据验证",
                data={"review_stage": "validator"},
            ),
        )
        validator_runs = self._execute_graph(validator_graph, session, persist_runs=False)
        validator_run = validator_runs[0] if validator_runs else None
        validator_report = (
            _parse_review(validator_run.output, "validator")
            if validator_run is not None and validator_run.status == "success"
            else {
                "valid": False,
                "role": "validator",
                "approved": False,
                "confidence": 0,
                "feedback": f"证据验证执行失败：{validator_run.error if validator_run else '没有返回结果'}",
                "findings": [],
            }
        )

        invalid_reports = [report for report in reports if not report.get("valid")]
        feedback_parts = [
            str(report.get("feedback") or "").strip()
            for report in reports + [validator_report]
            if str(report.get("feedback") or "").strip()
        ]
        approved = bool(
            validator_report.get("valid")
            and validator_report.get("approved")
            and validator_report.get("confidence", 0) >= cfg.min_confidence
        )
        if validator_report.get("valid") and validator_report.get("approved") and not approved:
            feedback_parts.insert(0, f"证据验证置信度低于阈值 {cfg.min_confidence}")
        if cfg.require_evidence and invalid_reports:
            approved = False
            feedback_parts.insert(0, "至少一个独立 reviewer 没有返回可验证的结构化报告")
        feedback = "；".join(feedback_parts)[:3000] or ("审查通过" if approved else "审查未通过")
        result = {
            "enabled": True,
            "approved": approved,
            "feedback": feedback,
            "reviewers": reports,
            "validator": validator_report,
            "min_confidence": cfg.min_confidence,
            "require_evidence": cfg.require_evidence,
        }
        self._emit(
            None,
            Event(
                kind="review_result",
                source="orchestrator",
                text=feedback[:500],
                data={
                    "review_stage": "complete",
                    "approved": approved,
                    "reviewer_count": len(reports),
                    "validator_valid": validator_report.get("valid", False),
                },
            ),
        )
        return result

    def _load_resume_graph(self, session: Session) -> CompiledGraph | None:
        """对异常退出或中断且计划签名匹配的会话恢复未完成任务。"""
        if session.status not in {"running", "stopped", "failed"} or not session.plan_signature:
            return None
        graph = self.store.load_plan(session.session_id)
        if graph is None or graph_signature(graph) != session.plan_signature:
            return None
        if not isinstance(session.task_runs, dict):
            logger.warning("会话 task_runs 不是对象，放弃恢复：%s", session.session_id)
            return None
        successful = sum(
            isinstance(item, dict) and item.get("status") == "success"
            for item in session.task_runs.values()
        )
        console.info(f"检测到可恢复任务图：{successful} 个任务已成功")
        return graph

    def _plan(self, goal: str, state: dict) -> Plan:
        return self.planner(goal, state)

    def _default_planner(self, goal: str, state: dict) -> Plan:
        ctx = f"（上一轮未达成，需改进：{state.get('feedback')}）" if state.get("feedback") else ""
        prompt = goal + ctx
        emit = lambda e: self._emit(None, e)  # noqa: E731
        try:
            return plan_with_llm(prompt, self.cfg, emit=emit, template=self.template)
        except LLMError as e:
            console.warn(f"任务拆分 LLM 不可用（{e}），交给单个 code agent 执行完整目标")
            return plan_with_single_code_agent(prompt, self.cfg, reason=str(e))

    def _execute(
        self,
        graph: CompiledGraph,
        session: Session,
        *,
        resume_runs: dict[str, dict] | None = None,
        persist_runs: bool = True,
    ) -> str:
        return self._summarize(
            self._execute_graph(graph, session, resume_runs=resume_runs, persist_runs=persist_runs)
        )

    def _execute_graph(
        self,
        graph: CompiledGraph,
        session: Session,
        *,
        resume_runs: dict[str, dict] | None = None,
        persist_runs: bool = True,
    ) -> list[TaskRun]:
        """统一执行入口：普通任务图与 evaluator 共用同一 session 工作区。"""
        workdir = str(self.store.workspace(session.session_id))

        def on_task_complete(run: TaskRun) -> None:
            if not persist_runs:
                return
            with self._persist_lock:
                session.task_runs[run.task.id] = task_run_to_dict(run)
                self.store.save(session, graph)

        ex = GraphExecutor(
            self.cfg, graph, self.broker, workdir=workdir, emit=self._emit,
            goal=session.goal, state=session.state,
            repository_dir=self.repository_dir,
            resume_runs=resume_runs,
            on_task_complete=on_task_complete,
        )
        self.current = ex
        try:
            return ex.execute()
        finally:
            self.current = None

    def send_message(self, target: str, text: str) -> list[str]:
        return self.current.send_message(target, text) if self.current else []

    def stop(self) -> None:
        self._stop_event.set()
        if self.current:
            self.current.stop()

    def _summarize(self, runs) -> str:
        parts = []
        for r in runs:
            parts.append(f"[{r.task.id} {r.task.executor}] {r.status}: {r.output[:1500]}")
        return "\n".join(parts)

    def _code_agent_evaluator(self, goal: str, state: dict, summary: str, session: Session) -> dict:
        node = SubTask(
            id="evaluate",
            title="目标达成判定",
            description=f"总体目标：{goal}\n\n执行结果摘要：\n{summary}\n\n累计状态：{json.dumps(state, ensure_ascii=False)[:2000]}\n\n{EVALUATOR_INSTRUCTION}",
            executor=self.cfg.goal_loop.evaluator,
            acceptance="输出严格 JSON {\"achieved\": bool, \"feedback\": str}",
            workspace_access="read_only",
            workdir_scope=self._evaluation_workdir_scope,
        )
        graph = CompiledGraph(nodes=[node], entry=node.id)
        console.step(f"启动 evaluator code agent [{self.cfg.goal_loop.evaluator}] 判定 goal")
        runs = self._execute_graph(graph, session, persist_runs=False)
        output = runs[0].output if runs else ""
        return _parse_verdict(output)
