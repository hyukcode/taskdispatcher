
from __future__ import annotations

import json
import threading

from . import console
from .config import Config
from .dispatch import build_single_task, is_short_circuit
from .graph_executor import GraphExecutor
from .llm import LLMError
from .models import CompiledGraph, GraphEdge, Plan, Session, SubTask
from .planner import _extract_json, plan_with_llm, plan_with_rules
from .session import SessionStore
from .template_compiler import compile_template

EVALUATOR_INSTRUCTION = """\
请先读取当前工作目录中的产物核实，再严格只输出 JSON（不要 markdown 代码块）：
{"achieved": true, "feedback": "……"}
- achieved=true 表示总体目标已达成；否则 false，feedback 写清还差什么、下一步要做什么。
"""


def plan_to_graph(plan: Plan, template: dict | None, cfg: Config | None = None) -> CompiledGraph:
    nodes = plan.tasks
    if not nodes:
        return CompiledGraph()

    has_deps = any(t.depends_on for t in nodes)
    if has_deps:
        edges = [GraphEdge(src=d, dst=t.id) for t in nodes for d in t.depends_on if d]
    else:
        edges = [GraphEdge(src=nodes[i].id, dst=nodes[i + 1].id) for i in range(len(nodes) - 1)]

    graph = CompiledGraph(nodes=nodes, edges=edges, entry=nodes[0].id)

    if template and cfg is not None:
        loop_info = (plan.orchestration or {}).get("loop") if hasattr(plan, "orchestration") else None
        tpl_graph = compile_template(cfg, template, loop_info=loop_info)
        template_nodes = {node.id: node for node in tpl_graph.nodes}
        for node in graph.nodes:
            compiled = template_nodes.get(node.id)
            if compiled is not None and node.internal_loop is None:
                node.internal_loop = compiled.internal_loop
    return graph


def _load_template(name: str) -> dict | None:
    try:
        from template import get_template

        tpl = get_template(name)
        if not tpl:
            return None
        tpl = dict(tpl)
        tpl.pop("_meta", None)
        return tpl
    except ImportError:
        return None
    except Exception as e:
        console.warn(f"模板加载失败（{name}）: {e}")
        return None


def _parse_verdict(output: str) -> dict:
    try:
        data = _extract_json(output)
        return {"achieved": bool(data.get("achieved", False)), "feedback": str(data.get("feedback", ""))}
    except Exception:
        return {"achieved": False, "feedback": f"（evaluator 未返回有效判定，原文：{output[:200]}）"}


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
        judge=None,
        template: dict | None = None,
    ):
        self.cfg = cfg
        self.broker = broker
        self.store = store
        self.emit = emit or (lambda run, event: console.event_line(event.summary(), source=event.source))
        self.planner = planner or self._default_planner
        self.evaluator = evaluator or (self._mock_evaluator if cfg.mock else self._code_agent_evaluator)
        self.judge = judge
        self.template = template
        self.session: Session | None = None
        self.current: GraphExecutor | None = None
        self._stop_event = threading.Event()

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
        max_iter = self.cfg.goal_loop.max_iterations

        while session.iteration < max_iter:
            if self._stop_event.is_set():
                break
            session.iteration += 1
            console.banner(f"goal 迭代 {session.iteration}/{max_iter}")

            plan = self._plan(goal, session.state)
            console.dim(f"拆分：{len(plan.tasks)} 个任务" + (f"（模板 {plan.template}）" if plan.template else ""))

            template = _load_template(plan.template) if plan.template else None
            graph = plan_to_graph(plan, template, self.cfg)

            summary = self._execute(graph, session)
            if self._stop_event.is_set():
                break

            verdict = self.evaluator(goal, session.state, summary, session)
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
            console.warn(f"已达 {max_iter} 轮仍未达成，交回 REPL 决策")
        return session

    def _plan(self, goal: str, state: dict) -> Plan:
        return self.planner(goal, state)

    def _default_planner(self, goal: str, state: dict) -> Plan:
        ctx = f"（上一轮未达成，需改进：{state.get('feedback')}）" if state.get("feedback") else ""
        prompt = goal + ctx
        emit = lambda e: self._emit(None, e)  # noqa: E731
        if self.cfg.mock:
            return plan_with_rules(prompt, template=self.template)
        try:
            return plan_with_llm(prompt, self.cfg, emit=emit, template=self.template)
        except LLMError as e:
            console.warn(f"任务拆分 LLM 不可用（{e}），回退规则拆分")
            return plan_with_rules(prompt, template=self.template)

    def _execute(self, graph: CompiledGraph, session: Session) -> str:
        workdir = str(self.store.workspace(session.session_id))

        if is_short_circuit(graph, self.cfg.dispatch.min_multiagent_steps):
            console.step(f"小任务短路：单 agent 直接执行（{len(graph.nodes)} 节点）")
            task = build_single_task(graph, goal=session.goal)
            short = CompiledGraph(nodes=[task], entry=task.id)
            ex = GraphExecutor(
                self.cfg, short, self.broker, workdir=workdir, emit=self._emit, goal=session.goal, state=session.state
            )
        else:
            ex = GraphExecutor(
                self.cfg, graph, self.broker, workdir=workdir, emit=self._emit,
                goal=session.goal, state=session.state, judge=self.judge,
            )
        self.current = ex
        try:
            runs = ex.execute()
        finally:
            self.current = None
        return self._summarize(runs)

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

    def _mock_evaluator(self, goal: str, state: dict, summary: str, session: Session) -> dict:
        return {"achieved": True, "feedback": "（mock 模式）判定达成"}

    def _code_agent_evaluator(self, goal: str, state: dict, summary: str, session: Session) -> dict:
        node = SubTask(
            id="evaluate",
            title="目标达成判定",
            description=f"总体目标：{goal}\n\n执行结果摘要：\n{summary}\n\n累计状态：{json.dumps(state, ensure_ascii=False)[:2000]}\n\n{EVALUATOR_INSTRUCTION}",
            executor=self.cfg.goal_loop.evaluator,
            acceptance="输出严格 JSON {\"achieved\": bool, \"feedback\": str}",
        )
        graph = CompiledGraph(nodes=[node], entry=node.id)
        console.step(f"启动 evaluator code agent [{self.cfg.goal_loop.evaluator}] 判定 goal")
        ex = GraphExecutor(
            self.cfg, graph, self.broker, workdir=str(self.store.workspace(session.session_id)),
            emit=self._emit, goal=goal, state=state,
        )
        self.current = ex
        try:
            runs = ex.execute()
        finally:
            self.current = None
        output = runs[0].output if runs else ""
        return _parse_verdict(output)
