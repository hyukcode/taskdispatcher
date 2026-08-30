from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import console
from .approvals import ApprovalPolicy
from .codex_app_server_runner import CodexAppServerRunner
from .config import Config
from .live import HELP, LiveTui
from .models import Event, Plan, TaskRun
from .sdk_runner import SdkClaudeRunner

EXECUTOR_TO_RUNNER = {"claude": SdkClaudeRunner, "codex": CodexAppServerRunner}


class Scheduler:
    def __init__(self, cfg: Config, prompt: str, plan: Plan, tui: LiveTui | None = None):
        self.cfg = cfg
        self.prompt = prompt
        self.plan = plan
        self.tui = tui or LiveTui()
        self.runs: dict[str, TaskRun] = {}
        self.approvals = ApprovalPolicy(cfg.approval)
        self._quit = threading.Event()
        self._active: dict[str, tuple] = {}  # task_id -> (runner, thread)
        self._active_lock = threading.Lock()
        self._run_id = time.strftime("%Y%m%d-%H%M%S")
        self._raw_dir = self.cfg.workspace_path / self._run_id

    def _on_event(self, run: TaskRun, event: Event) -> None:
        self._raw_append(run, event)
        self.tui.emit(run, event)

        if event.kind == "permission_request":
            if self.cfg.approval.mode == "ask_console":
                self.tui.hold_for_approval()
            with self._active_lock:
                runner = self._active.get(run.task.id, (None, None))[0]
            if not getattr(runner, "self_handles_approval", False):
                self.approvals.handle(run, event, emit=self._emit_decision, send_msg=self._sender(run, runner))
        elif event.kind == "permission_result":
            if self.tui.is_held:
                self.tui.release_hold()

    def _emit_decision(self, run: TaskRun, event: Event) -> None:
        self._raw_append(run, event)
        self.tui.emit(run, event)

    def _sender(self, run: TaskRun, runner):
        def send(text: str):
            if runner is not None:
                return runner.send_message(text)
            return False

        return send

    def _raw_append(self, run: TaskRun, event: Event) -> None:
        try:
            self._raw_dir.mkdir(parents=True, exist_ok=True)
            f = self._raw_dir / f"{run.task.id}.events.jsonl"
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": event.ts, "kind": event.kind, "source": event.source, "text": event.text, "data": event.data}, ensure_ascii=False) + "\n")
            if not run.raw_log_path:
                run.raw_log_path = str(f)
        except Exception:
            pass

    def run(self) -> list[TaskRun]:
        console.banner("计划")
        print(f"目标: {self.plan.objective}")
        if self.plan.rationale:
            print(f"说明: {self.plan.rationale}")
        for t in self.plan.tasks:
            deps = ",".join(t.depends_on) or "—"
            print(f"  {t.id} [{t.executor}] ← {deps}  {t.title}")
        print()

        from .planner import levels

        layers = levels(self.plan)
        self.tui.start()
        try:
            for idx, layer in enumerate(layers, start=1):
                if self._quit.is_set():
                    break
                console.banner(f"执行批次 {idx}/{len(layers)}（{len(layer)} 个任务并行）")
                self._run_layer(layer)
        finally:
            self.tui.stop()
        return list(self.runs.values())

    def _run_layer(self, layer: list) -> None:
        runnable = []
        for task in layer:
            blocked = [
                dep for dep in task.depends_on
                if dep not in self.runs or self.runs[dep].status != "success"
            ]
            if blocked:
                self._skip_task(task, blocked)
            else:
                runnable.append(task)

        threads: list[threading.Thread] = []
        for task in runnable:
            if self._quit.is_set():
                break
            th = threading.Thread(target=self._task_worker, args=(task,), daemon=True, name=f"task-{task.id}")
            th.start()
            threads.append(th)

        while any(t.is_alive() for t in threads) and not self._quit.is_set():
            self._poll_input()
            time.sleep(0.1)
        for task in runnable:
            with self._active_lock:
                runner, _th = self._active.get(task.id, (None, None))
            if runner is not None and runner.is_alive():
                runner.stop()

    def _skip_task(self, task, blocked: list[str]) -> None:
        reason = "前置任务未成功：" + ", ".join(blocked)
        run = TaskRun(task=task, workdir=str(self._task_workdir(task)), status="skipped", error=reason)
        self.runs[task.id] = run
        console.status_line("↷", f"跳过 {task.id} [{task.executor}]：{reason}", "yellow")
        self._on_event(run, Event(kind="error", source="orchestrator", text=reason, data={"skipped": True}))

    def _task_worker(self, task) -> None:
        workdir = self._task_workdir(task)
        run = TaskRun(task=task, workdir=str(workdir))
        self.runs[task.id] = run
        runner_cls = EXECUTOR_TO_RUNNER.get(task.executor)
        if runner_cls is None:
            run.status = "failed"
            run.error = f"未知 executor: {task.executor}"
            return
        runner = runner_cls(self.cfg, run, str(workdir), self._on_event, self._prompt_for(task))
        with self._active_lock:
            self._active[task.id] = (runner, threading.current_thread())
        console.status_line("▶", f"启动 {task.id} [{task.executor}] {task.title}", "blue")
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
            if run.status != "failed":
                run.status = "success" if run.exit_code == 0 else ("failed" if run.exit_code else "running")
            if run.status == "running":
                run.status = "success" if run.exit_code == 0 else "failed"
        except FileNotFoundError as e:
            run.status = "failed"
            run.error = f"找不到可执行文件: {e}"
            self._on_event(run, Event(kind="error", source=task.executor, text=f"启动失败: {e}"))
        except Exception as e:
            run.status = "failed"
            run.error = str(e)
            self._on_event(run, Event(kind="error", source=task.executor, text=f"运行异常: {e}"))
        finally:
            runner.stop()
            run.ended_at = time.time()
            with self._active_lock:
                self._active.pop(task.id, None)
            if run.status == "success":
                console.status_line("✓", f"{task.id} [{task.executor}] 完成（{run.duration:.1f}s, ${run.cost_usd:.4f}）", "green")
            else:
                console.status_line("✗", f"{task.id} [{task.executor}] {run.status}: {run.error or '见上方输出'}", "red")

    def _task_workdir(self, task) -> Path:
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        return self._raw_dir

    def _prompt_for(self, task) -> str:
        parts = [
            f"任务 {task.id}: {task.title}",
            task.description,
            f"共享工作目录：{self._raw_dir}\n请先读取该目录中前置 agent 已产生的代码和文件，再继续本任务；不要重复从零分析整个模板。",
        ]
        if getattr(task, "internal_loop", None) is not None and task.internal_loop.enabled:
            loop = task.internal_loop
            loop_prompt = (
                f"本任务包含内部迭代：最多执行 {loop.max_iterations} 轮。"
                "请在当前任务内部完成检查、修正、重试，不要让编排器重新启动整个任务图。"
            )
            if loop.exit_condition:
                loop_prompt += f"\n内部循环退出条件：{loop.exit_condition}"
            if loop.feedback_prompt:
                loop_prompt += f"\n迭代要求：{loop.feedback_prompt}"
            parts.append(loop_prompt)
        if task.acceptance:
            parts.append(f"完成标准: {task.acceptance}")
        deps = [self.runs[d].output for d in task.depends_on if d in self.runs and self.runs[d].output]
        if deps:
            parts.append("\n依赖任务输出（作为上下文）：\n" + "\n---\n".join(deps)[:8000])
        dep_ids = [d for d in task.depends_on if d in self.runs and self.runs[d].status == "success"]
        if dep_ids:
            parts.append(
                "\n注意：前置任务 (" + ", ".join(dep_ids) + ") 的文件产物已落在当前工作目录中，"
                "请直接读取使用，无需重复生成。"
            )
        return "\n\n".join(parts)

    def _poll_input(self) -> None:
        if not self.tui:
            return
        for cmd in self.tui.poll_commands():
            try:
                self._handle_command(cmd)
            except Exception as e:  # noqa: BLE001
                console.error(f"指令处理出错: {e}")

    def _handle_command(self, cmd: dict) -> None:
        ctype = cmd["type"]
        if ctype == "noop":
            return
        if ctype == "msg":
            self._route_message(cmd["target"], cmd["text"])
            return
        c = cmd["cmd"]
        arg = cmd["arg"]
        if c in ("quit", "q", "exit"):
            console.warn("终止所有运行中的 agent…")
            self._quit.set()
            with self._active_lock:
                runners = list(self._active.values())
            for runner, _ in runners:
                runner.stop()
        elif c == "help":
            self.tui.print_raw(HELP)
        elif c == "status":
            self._print_status()
        elif c == "plan":
            for t in self.plan.tasks:
                print(f"  {t.id} [{t.executor}] ← {','.join(t.depends_on) or '—'}  {t.title}")
        elif c == "pause":
            self.tui.pause()
            console.dim("输入转发已暂停（:resume 恢复）")
        elif c == "resume":
            self.tui.resume()
            console.dim("已恢复输入转发")
        elif c == "allow" or c == "deny":
            allowed = c == "allow"
            self._decide_approval(arg, allowed)
        elif c == "done":
            self._finalize_task(arg)
        elif c == "attach":
            console.info("pty attach 是独立子命令：Ctrl-C 结束当前调度后运行 `tasker attach <claude|codex>`")
        else:
            console.dim(f"未知指令 :{c}，输入 :help 查看")

    def _route_message(self, target: str, text: str) -> None:
        if not text:
            return
        matched: list[str] = []
        with self._active_lock:
            items = list(self._active.items())
        for tid, (runner, _) in items:
            task = self.runs[tid].task
            if target == "all" or target == task.executor or target == tid or target == tid.lstrip("t"):
                ok = runner.send_message(text)
                matched.append(tid)
                if not ok:
                    console.dim(f"[{tid}] 消息未能注入（runner 当前不可注入或已结束）")
        if not matched:
            with self._active_lock:
                active_ids = list(self._active.keys())
            console.warn(f"没有匹配的 agent（target={target}）。当前活跃: {active_ids}")

    def _decide_approval(self, arg: str, allowed: bool) -> None:
        req_id = arg or self._find_pending_approval_id()
        if not req_id:
            console.warn("当前没有待处理的审批请求")
            return
        with self._active_lock:
            runners = list(self._active.values())
        for runner, _ in runners:
            if hasattr(runner, "approval_respond") and runner.approval_respond(req_id, allowed):
                self.tui.release_hold()
                return
        ok = self.approvals.decide(req_id, allowed, emit=self._emit_decision, send_msg=self._last_sender())
        if ok:
            self.tui.release_hold()
        if not ok:
            console.warn(f"审批请求 {req_id} 已不在待处理列表（可能已由策略自动处理）")

    def _find_pending_approval_id(self) -> str:
        with self._active_lock:
            runners = list(self._active.values())
        for runner, _ in runners:
            if hasattr(runner, "pending_approval_ids") and runner.pending_approval_ids:
                return list(runner.pending_approval_ids)[-1]
        return next(reversed(list(self.approvals.pending)), "") if self.approvals.pending else ""

    def _finalize_task(self, arg: str) -> None:
        with self._active_lock:
            keys = list(self._active.keys())
        target = arg or (keys[0] if keys else "")
        with self._active_lock:
            item = self._active.get(target)
        runner, _th = item if item else (None, None)
        if runner is None:
            with self._active_lock:
                active_ids = list(self._active.keys())
            console.warn(f"没有运行中的任务 {target or '?'}；活跃: {active_ids}")
            return
        if hasattr(runner, "finalize"):
            console.info(f"手动收尾 {target} …")
            runner.finalize()

    def _last_sender(self):
        with self._active_lock:
            items = list(self._active.items())
        for tid, (runner, _) in items:
            run = self.runs.get(tid)
            if run:
                return self._sender(run, runner)
        return None

    def _print_status(self) -> None:
        console.banner("任务状态")
        for tid, run in self.runs.items():
            stats = {
                "思考": len([e for e in run.events if e.kind == "thinking"]),
                "工具调用": len([e for e in run.events if e.kind == "tool_use"]),
                "工具结果": len([e for e in run.events if e.kind == "tool_result"]),
                "审批": len([e for e in run.events if e.kind in ("permission_request", "permission_result")]),
                "注入消息": len([e for e in run.events if e.kind == "user_message"]),
            }
            self.tui.print_raw(f"  {tid} [{run.task.executor}] {run.status:<8} {run.duration:6.1f}s ${run.cost_usd:.4f}  {stats}")
