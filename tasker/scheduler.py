"""调度器：按依赖分层并发执行 claude/codex，实时流式输出事件，
并处理用户在 CLI 里随时注入的消息 / 审批决定。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import console
from .approvals import ApprovalPolicy
from .claude_runner import ClaudeRunner
from .codex_app_server_runner import CodexAppServerRunner
from .codex_runner import CodexRunner
from .config import Config
from .live import HELP, LiveTui
from .models import Event, Plan, TaskRun

EXECUTOR_TO_RUNNER = {"claude": ClaudeRunner, "codex": CodexRunner, "codex-app-server": CodexAppServerRunner}


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
        self._run_id = time.strftime("%Y%m%d-%H%M%S")
        self._raw_dir = self.cfg.workspace_path / self._run_id

    # ---------------- 事件回调 ----------------
    def _on_event(self, run: TaskRun, event: Event) -> None:
        self._raw_append(run, event)
        self.tui.emit(run, event)
        if event.kind == "permission_request":
            runner = self._active.get(run.task.id, (None, None))[0]
            # 使用 can_use_tool 自管理审批的 runner（如 SdkClaudeRunner）不再走 ApprovalPolicy
            if not getattr(runner, "self_handles_approval", False):
                self.approvals.handle(run, event, emit=self._emit_decision, send_msg=self._sender(run, runner))

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

    # ---------------- 主流程 ----------------
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
        threads: list[threading.Thread] = []
        for task in layer:
            if self._quit.is_set():
                break
            th = threading.Thread(target=self._task_worker, args=(task,), daemon=True, name=f"task-{task.id}")
            th.start()
            threads.append(th)

        # 主循环：转发用户输入，直到本层全部结束
        while any(t.is_alive() for t in threads) and not self._quit.is_set():
            self._poll_input()
            time.sleep(0.1)
        # 清理仍在运行的 runner（如超时/被中断）
        for task in layer:
            runner, _th = self._active.get(task.id, (None, None))
            if runner is not None and runner.is_alive():
                runner.stop()

    # ---------------- 单任务 worker ----------------
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
        self._active[task.id] = (runner, threading.current_thread())
        console.status_line("▶", f"启动 {task.id} [{task.executor}] {task.title}", "blue")
        try:
            runner.start()
            deadline = time.time() + self.cfg.timeout_per_task
            while not runner.is_done() and not self._quit.is_set():
                if time.time() > deadline:
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
        except Exception as e:  # noqa: BLE001
            run.status = "failed"
            run.error = str(e)
            self._on_event(run, Event(kind="error", source=task.executor, text=f"运行异常: {e}"))
        finally:
            runner.stop()
            run.ended_at = time.time()
            self._active.pop(task.id, None)
            if run.status == "success":
                console.status_line("✓", f"{task.id} [{task.executor}] 完成（{run.duration:.1f}s, ${run.cost_usd:.4f}）", "green")
            else:
                console.status_line("✗", f"{task.id} [{task.executor}] {run.status}: {run.error or '见上方输出'}", "red")

    def _task_workdir(self, task) -> Path:
        base = self._raw_dir / task.id
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _prompt_for(self, task) -> str:
        parts = [f"任务 {task.id}: {task.title}", task.description]
        if task.acceptance:
            parts.append(f"完成标准: {task.acceptance}")
        deps = [self.runs[d].output for d in task.depends_on if d in self.runs and self.runs[d].output]
        if deps:
            parts.append("\n依赖任务输出（作为上下文）：\n" + "\n---\n".join(deps)[:8000])
        return "\n\n".join(parts)

    # ---------------- 用户输入路由 ----------------
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
            for runner, _ in self._active.values():
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
        for tid, (runner, _) in self._active.items():
            task = self.runs[tid].task
            if target == "all" or target == task.executor or target == tid or target == tid.lstrip("t"):
                ok = runner.send_message(text)
                matched.append(tid)
                if not ok:
                    console.dim(f"[{tid}] 消息未能注入（runner 当前不可注入或已结束）")
        if not matched:
            console.warn(f"没有匹配的 agent（target={target}）。当前活跃: {[t for t in self._active]}")

    def _decide_approval(self, arg: str, allowed: bool) -> None:
        req_id = arg or self._find_pending_approval_id()
        if not req_id:
            console.warn("当前没有待处理的审批请求")
            return
        # 优先路由到自管理审批的 runner（SdkClaudeRunner.can_use_tool 的等待）
        for runner, _ in self._active.values():
            if hasattr(runner, "approval_respond") and runner.approval_respond(req_id, allowed):
                return
        ok = self.approvals.decide(req_id, allowed, emit=self._emit_decision, send_msg=self._last_sender())
        if not ok:
            console.warn(f"审批请求 {req_id} 已不在待处理列表（可能已由策略自动处理）")

    def _find_pending_approval_id(self) -> str:
        # 优先找 SDK runner 自管理的待审批请求
        for runner, _ in self._active.values():
            if hasattr(runner, "pending_approval_ids") and runner.pending_approval_ids:
                return list(runner.pending_approval_ids)[-1]
        return next(reversed(list(self.approvals.pending)), "") if self.approvals.pending else ""

    def _finalize_task(self, arg: str) -> None:
        """:done [<taskid>] —— 手动收尾一个仍在等待的任务（关闭其 stdin 让进程退出）。"""
        target = arg or (next(iter(self._active), "") if self._active else "")
        runner, _th = self._active.get(target, (None, None))
        if runner is None:
            console.warn(f"没有运行中的任务 {target or '?'}；活跃: {list(self._active)}")
            return
        if hasattr(runner, "finalize"):
            console.info(f"手动收尾 {target} …")
            runner.finalize()

    def _last_sender(self):
        for tid, (runner, _) in self._active.items():
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
            print(f"  {tid} [{run.task.executor}] {run.status:<8} {run.duration:6.1f}s ${run.cost_usd:.4f}  {stats}")
