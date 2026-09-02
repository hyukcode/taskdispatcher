
from __future__ import annotations

import os
from pathlib import Path
import threading
import time

from . import console
from .approvals import ApprovalBroker
from .config import Config
from .goal_loop import GoalLoop
from .live import HELP, LiveTui
from .models import Session
from .session import SessionStore, new_session_id

_CODE_SUFFIXES = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".h", ".cc", ".cpp",
    ".cxx", ".cs", ".rb", ".php", ".swift", ".kt", ".kts", ".scala", ".sh", ".ps1", ".sql",
    ".vue", ".svelte", ".html", ".css",
})
_CODE_MARKERS = frozenset({
    "Dockerfile", "Makefile", "pyproject.toml", "setup.py", "package.json", "Cargo.toml", "go.mod",
    "pom.xml", "build.gradle", "requirements.txt",
})
_IGNORED_DIRS = frozenset({
    ".git", ".tasker", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "dist", "build", "workspaces", "reports",
})


def _has_code_files(root: Path) -> bool:
    """检测目录是否像代码仓库，忽略依赖、缓存和构建产物。"""
    try:
        for _current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            dirs[:] = [name for name in dirs if name not in _IGNORED_DIRS and not name.startswith(".venv")]
            if any(name in _CODE_MARKERS or Path(name).suffix.lower() in _CODE_SUFFIXES for name in files):
                return True
    except OSError:
        return False
    return False

REPL_HELP = """\
命令（Idle 提示符 tasker> 下）：

  <目标>                输入目标，开始编排执行
  /new <目标>            开启一个新的任务会话
  /plan <目标>          只拆分计划，不执行
  /sessions             列出历史会话
  /status               显示当前会话的子任务状态
  /resume <session_id>  恢复中断任务或开启下一轮 goal loop
  /continue             当前会话再跑一轮
  /restart <task_id>    从指定子任务重新执行，并重跑其下游任务
  /delete <session_id>  逻辑删除会话（保留计划、事件和工作区）
  /restore <session_id> 恢复逻辑删除的会话
  /replan               清空状态重新拆分当前目标
  /config               显示当前配置摘要
  /help                 显示本帮助
  /quit                 退出（也可直接输入 quit / q / exit，或 Ctrl+C / Ctrl+D）
"""


_STATUS_MARKS = {
    "pending": ("·", "dim"),
    "running": ("⟳", "blue"),
    "success": ("✓", "green"),
    "failed": ("✗", "red"),
    "skipped": ("↷", "yellow"),
    "stopped": ("■", "yellow"),
}


def _status_mark(status: str) -> str:
    return _STATUS_MARKS.get(status, ("?", "dim"))[0]


def _status_color(status: str) -> str:
    return _STATUS_MARKS.get(status, ("?", "dim"))[1]


def _short_text(value: str, width: int = 180) -> str:
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


class Repl:
    def __init__(self, cfg: Config, *, planner=None, evaluator=None, template=None):
        self.cfg = cfg
        self.store = SessionStore(cfg.session)
        self.broker = ApprovalBroker(cfg.approval)
        self.session: Session | None = None
        self.planner = planner
        self.evaluator = evaluator
        self.template = template
        self._quit_requested = False  
        self.launch_dir = Path.cwd().expanduser().resolve()
        self.repository_dir: Path | None = None

    def run(self) -> int:
        if not self._ensure_repository_dir():
            return 1
        console.banner("tasker 交互式编排器")
        console.dim("输入目标开始编排；/help 查看命令；/quit 退出")
        while True:
            try:
                line = input("tasker> ").strip().lstrip("﻿")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line.startswith("/"):
                if not self._idle_command(line):
                    break
            elif line.lower() in ("quit", "q", "exit"):
                break
            else:
                self._run_goal(line)
                if self._quit_requested:
                    self._quit_requested = False
                    break
        return 0

    def _ensure_repository_dir(self) -> bool:
        """优先使用 tasker 启动目录；无代码时在进入 REPL 前询问仓库。"""
        if self.repository_dir is not None:
            return True
        if _has_code_files(self.launch_dir):
            self.repository_dir = self.launch_dir
            return True

        console.warn(f"当前启动目录没有检测到代码文件：{self.launch_dir}")
        console.info("代码修改、测试和编译任务需要仓库目录；中间日志/事件仍保存到 session workspace。")
        while True:
            try:
                raw = input("请输入代码仓库目录（留空取消）：").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            if not raw:
                console.warn("未提供仓库目录，已取消进入 REPL")
                return False
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = self.launch_dir / candidate
            try:
                candidate = candidate.resolve()
            except OSError:
                candidate = candidate.absolute()
            if candidate.is_dir() and _has_code_files(candidate):
                self.repository_dir = candidate
                console.info(f"代码任务将使用仓库目录：{candidate}")
                return True
            console.warn(f"目录不存在或没有检测到代码文件，请重试：{candidate}")

    def _idle_command(self, line: str) -> bool:
        parts = line[1:].split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("quit", "q", "exit"):
            return False
        if cmd == "help":
            print(REPL_HELP)
        elif cmd == "new":
            self._new(arg)
        elif cmd == "sessions":
            self._list_sessions()
        elif cmd == "status":
            self._show_status()
        elif cmd == "resume":
            self._resume(arg)
        elif cmd == "continue":
            self._continue()
        elif cmd == "restart":
            self._restart(arg)
        elif cmd == "delete":
            self._delete(arg)
        elif cmd == "restore":
            self._restore(arg)
        elif cmd == "replan":
            self._replan()
        elif cmd == "plan":
            self._show_plan(arg)
        elif cmd == "config":
            self._show_config()
        else:
            console.dim(f"未知命令 /{cmd}，输入 /help 查看")
        return True

    def _list_sessions(self) -> None:
        rows = self.store.list()
        if not rows:
            console.dim("暂无历史会话")
            return
        for r in rows:
            print(f"  {r['session_id']}  [{r['status']}]  iter={r['iteration']}  {r['goal']}")
            print(f"       updated={r['updated_at']}")

    def _resume(self, session_id: str) -> None:
        if not session_id:
            console.warn("用法: /resume <session_id>")
            return
        session = self.store.load(session_id)
        if session is None:
            console.warn(f"会话不存在: {session_id}")
            return
        if session.status == "goal_achieved":
            console.info(f"会话 {session_id} 已完成，无需恢复；如需重新执行请输入新目标")
            return
        if session.status == "deleted":
            console.warn(f"会话 {session_id} 已逻辑删除，请先执行 /restore {session_id}")
            return
        console.info(f"恢复会话 {session_id}（goal: {session.goal}，iter={session.iteration}）")
        self._run_goal(session.goal, session)

    def _continue(self) -> None:
        if self.session is None:
            console.warn("当前没有活动会话，先输入目标或 /resume")
            return
        if self.session.status == "deleted":
            console.warn(f"当前会话已逻辑删除，请先执行 /restore {self.session.session_id}")
            return
        if self.session.status == "goal_achieved":
            console.info("当前会话已经完成；如需重新执行某个子任务，请使用 /restart <task_id>")
            return
        if self.session.status not in {"paused", "stopped", "failed"}:
            console.warn(f"当前会话状态为 {self.session.status}，暂时不能恢复")
            return
        console.info(
            f"继续会话 {self.session.session_id}：保留成功任务，重新执行未完成任务"
        )
        self._run_goal(self.session.goal, self.session)

    def _new(self, goal: str) -> None:
        if not goal:
            console.warn("用法: /new <目标>")
            return
        console.info("开启新的任务会话")
        self._run_goal(goal)

    def _resolve_task_id(self, graph, raw_task_id: str) -> str | None:
        task_id = raw_task_id.strip()
        ids = {node.id for node in graph.nodes}
        if task_id in ids:
            return task_id
        if task_id.isdigit() and f"t{task_id}" in ids:
            return f"t{task_id}"
        return None

    @staticmethod
    def _descendants(graph, task_id: str) -> set[str]:
        successors: dict[str, set[str]] = {node.id: set() for node in graph.nodes}
        for node in graph.nodes:
            for dependency in node.depends_on:
                successors.setdefault(dependency, set()).add(node.id)
        for edge in graph.edges:
            successors.setdefault(edge.src, set()).add(edge.dst)

        result = {task_id}
        pending = [task_id]
        while pending:
            current = pending.pop()
            for child in successors.get(current, set()):
                if child not in result:
                    result.add(child)
                    pending.append(child)
        return result

    def _restart(self, raw_task_id: str) -> None:
        if self.session is None:
            console.warn("当前没有活动会话，先输入目标或 /resume")
            return
        if not raw_task_id:
            console.warn("用法: /restart <task_id>（例如 /restart t3）")
            return
        if self.session.status == "deleted":
            console.warn(f"当前会话已逻辑删除，请先执行 /restore {self.session.session_id}")
            return
        graph = self.store.load_plan(self.session.session_id)
        if graph is None:
            console.warn("当前会话没有可恢复的任务图")
            return
        task_id = self._resolve_task_id(graph, raw_task_id.split()[0])
        if task_id is None:
            console.warn(f"任务不存在: {raw_task_id}; 可用任务: {', '.join(node.id for node in graph.nodes)}")
            return

        invalidated = self._descendants(graph, task_id)
        for node_id in invalidated:
            self.session.task_runs.pop(node_id, None)
        self.session.status = "stopped"
        self.session.state["manual_restart"] = {
            "task_id": task_id,
            "invalidated_tasks": [node.id for node in graph.nodes if node.id in invalidated],
        }
        self.session.history.append(
            {
                "type": "manual_restart",
                "task_id": task_id,
                "invalidated_tasks": [node.id for node in graph.nodes if node.id in invalidated],
            }
        )
        self.store.save(self.session, graph)
        rerun_ids = [node.id for node in graph.nodes if node.id in invalidated]
        console.info(
            f"从 {task_id} 重新执行：{', '.join(rerun_ids)}；其上游成功任务将复用原结果"
        )
        self._run_goal(self.session.goal, self.session)

    def _delete(self, session_id: str) -> None:
        if not session_id:
            console.warn("用法: /delete <session_id>")
            return
        session = self.store.soft_delete(session_id)
        if session is None:
            console.warn(f"会话不存在: {session_id}")
            return
        if self.session and self.session.session_id == session_id:
            self.session = session
        console.info(f"会话 {session_id} 已逻辑删除（数据仍保留，可 /restore 恢复）")

    def _restore(self, session_id: str) -> None:
        if not session_id:
            console.warn("用法: /restore <session_id>")
            return
        existing = self.store.load(session_id)
        if existing is None:
            console.warn(f"会话不存在: {session_id}")
            return
        if existing.status != "deleted":
            console.info(f"会话 {session_id} 未处于逻辑删除状态")
            return
        session = self.store.restore(session_id)
        if session is None:
            console.warn(f"会话不存在: {session_id}")
            return
        console.info(f"会话 {session_id} 已恢复，可执行 /resume {session_id}")

    def _replan(self) -> None:
        if self.session is None:
            console.warn("当前没有活动会话")
            return
        self.session.state.clear()
        self.session.iteration = 0
        console.info("已清空状态，重新拆分")
        self._run_goal(self.session.goal, self.session)

    def _show_plan(self, goal: str) -> None:
        if not goal:
            console.warn("用法: /plan <目标>")
            return
        from .llm import LLMError
        from .planner import plan_with_llm, plan_with_rules

        try:
            plan = plan_with_llm(goal, self.cfg, emit=lambda e: None, template=self.template)
        except LLMError as e:
            console.warn(f"拆分 LLM 不可用（{e}），用规则拆分")
            plan = plan_with_rules(goal, template=self.template)
        print(f"目标: {plan.objective}")
        if plan.template:
            print(f"模板: {plan.template}")
        for t in plan.tasks:
            print(f"  {t.id} [{t.executor}] [{t.workdir_scope}] ← {','.join(t.depends_on) or '—'}  {t.title}")

    def _session_status_rows(self, session: Session, graph=None) -> list[dict]:
        nodes = list(graph.nodes) if graph is not None else []
        saved = session.task_runs if isinstance(session.task_runs, dict) else {}
        saved_nodes = []
        if not nodes:
            for task_id, snapshot in saved.items():
                task = snapshot.get("task", {}) if isinstance(snapshot, dict) else {}
                saved_nodes.append(
                    {
                        "id": task_id,
                        "title": task.get("title", task_id),
                        "executor": task.get("executor", "?"),
                        "depends_on": task.get("depends_on", []),
                    }
                )

        rows: list[dict] = []
        for node in nodes or saved_nodes:
            node_id = node["id"] if isinstance(node, dict) else node.id
            title = node.get("title", node_id) if isinstance(node, dict) else node.title
            executor = node.get("executor", "?") if isinstance(node, dict) else node.executor
            depends_on = node.get("depends_on", []) if isinstance(node, dict) else node.depends_on
            snapshot = saved.get(node_id)
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            started = float(snapshot.get("started_at", 0.0) or 0.0)
            ended = float(snapshot.get("ended_at", 0.0) or 0.0)
            duration = max(0.0, ended - started) if started and ended else 0.0
            attempts = snapshot.get("attempts") or []
            rows.append(
                {
                    "id": node_id,
                    "title": title,
                    "executor": executor,
                    "status": str(snapshot.get("status", "pending") or "pending"),
                    "active": False,
                    "duration": duration,
                    "attempts": len(attempts) or (1 if started else 0),
                    "injections": 0,
                    "output": str(snapshot.get("output", "") or ""),
                    "error": str(snapshot.get("error", "") or ""),
                    "workdir": str(snapshot.get("workdir", "") or ""),
                    "raw_log_path": str(snapshot.get("raw_log_path", "") or ""),
                    "depends_on": list(depends_on or []),
                }
            )
        return rows

    @staticmethod
    def _print_task_rows(rows: list[dict], *, include_output: bool, writer=None) -> None:
        lines = Repl._task_row_lines(rows, include_output=include_output)
        text = "\n".join(lines)
        if writer is not None:
            writer(text)
        else:
            for line in lines:
                print(line)

    @staticmethod
    def _task_row_lines(rows: list[dict], *, include_output: bool) -> list[str]:
        lines: list[str] = []
        if not rows:
            return [console.paint("暂无子任务执行记录", "dim", "grey")]
        for row in rows:
            status = row.get("status", "pending")
            mark = _status_mark(status)
            color = _status_color(status)
            active = " · active" if row.get("active") else ""
            duration = row.get("duration", 0.0) or 0.0
            attempts = row.get("attempts", 0) or 0
            injections = row.get("injections", 0) or 0
            meta = f"{row['id']}  {row.get('title', '')}  [{row.get('executor', '?')}]"
            meta += f"  {status}{active}  {duration:.1f}s"
            if attempts > 1:
                meta += f"  attempts={attempts}"
            if injections:
                meta += f"  injected={injections}"
            lines.append("  " + console.paint(f"{mark} {meta}", color))
            if row.get("depends_on"):
                lines.append(f"      depends_on: {','.join(row['depends_on'])}")
            if not include_output:
                continue
            output = str(row.get("output", "") or "").strip()
            error = str(row.get("error", "") or "").strip()
            if output:
                output_lines = output.splitlines()
                result_lines = [f"      交付结果: {_short_text(output_lines[0], 220)}"]
                result_lines.extend(f"          {_short_text(line, 220)}" for line in output_lines[1:3])
                if len(output_lines) > 3:
                    result_lines.append("          …（完整结果见事件日志）")
                lines.extend(result_lines)
            if error:
                lines.append(f"      错误: {console.paint(_short_text(error, 220), 'red')}")
            if row.get("workdir"):
                lines.append(f"      工作目录: {row['workdir']}")
            if row.get("raw_log_path"):
                lines.append(f"      原始日志: {row['raw_log_path']}")
        return lines

    def _show_status(self, loop: GoalLoop | None = None, tui: LiveTui | None = None) -> None:
        if loop is not None and loop.current is not None:
            rows = loop.current.status_snapshot()
            text = "实时任务状态\n" + "\n".join(self._task_row_lines(rows, include_output=False))
            if tui is not None:
                tui.print_raw(text)
            else:
                console.banner("实时任务状态")
                self._print_task_rows(rows, include_output=False)
            return
        if self.session is None:
            console.warn("当前没有活动会话，先输入目标或 /resume")
            return
        graph = self.store.load_plan(self.session.session_id)
        console.banner(f"会话状态 · {self.session.session_id}")
        print(f"目标: {self.session.goal}")
        print(f"会话状态: {self.session.status}  iteration={self.session.iteration}")
        self._print_task_rows(self._session_status_rows(self.session, graph), include_output=False)

    def _show_config(self) -> None:
        c = self.cfg
        print(f"display.level={c.display.level}   max_parallel={c.max_parallel}")
        print(f"goal_loop.max_iterations={c.goal_loop.max_iterations}   evaluator={c.goal_loop.evaluator}")
        print(f"approval.mode={c.approval.mode}")
        print(f"session.dir={c.session.path}")

    def _run_goal(self, goal: str, session: Session | None = None) -> None:
        if not self._ensure_repository_dir():
            return
        if session is None:
            session = Session(session_id=new_session_id(), goal=goal)
        self.session = session
        self.store.save(session)
        console.banner("执行目标")
        print(f"目标: {goal}")
        print(f"会话: {session.session_id}")
        print()

        think_level = "off" if self.cfg.display.level == "minimal" else "full"
        tui = LiveTui(think_level=think_level, display_level=self.cfg.display.level)
        loop = GoalLoop(
            self.cfg, self.broker, self.store, emit=tui.emit,
            planner=self.planner, evaluator=self.evaluator,
            template=self.template,
            repository_dir=str(self.repository_dir or self.launch_dir),
        )

        def worker() -> None:
            loop.run_goal(goal, session)

        th = threading.Thread(target=worker, daemon=True, name="goal-loop")
        th.start()

        tui.start()
        try:
            while th.is_alive():
                for cmd in tui.poll_commands():
                    self._handle_command(cmd, loop, tui)
                time.sleep(0.1)
        finally:
            tui.stop()
        th.join()

        self._show_final(session)

    def _handle_command(self, cmd: dict, loop: GoalLoop, tui: LiveTui) -> None:
        ctype = cmd["type"]
        if ctype == "noop":
            return
        if ctype == "msg":
            matched = loop.send_message(cmd["target"], cmd["text"])
            if not matched:
                console.warn("没有匹配的活跃 agent（或已结束）")
            else:
                console.info(f"消息已注入: {', '.join(matched)}")
            return

        c = cmd["cmd"]
        arg = cmd["arg"]
        if c in ("allow", "deny"):
            req_id = arg or self.broker.find_pending_id(kind="permission")
            if not req_id:
                console.warn("没有待处理的工具审批请求")
                return
            self.broker.resolve(req_id, allowed=(c == "allow"))
            tui.release_hold()
        elif c == "approve":
            req_id = arg or self.broker.find_pending_id(kind="review")
            if not req_id:
                console.warn("没有待处理的人工审查点")
                return
            self.broker.resolve(req_id, allowed=True)
            tui.release_hold()
        elif c == "reject":
            req_id = self.broker.find_pending_id(kind="review")
            if not req_id:
                console.warn("没有待处理的人工审查点")
                return
            self.broker.resolve(req_id, allowed=False, feedback=arg)
            tui.release_hold()
        elif c in ("quit", "q", "exit"):
            console.warn("终止执行并退出…")
            self._quit_requested = True
            loop.stop()
        elif c == "help":
            tui.print_raw(HELP)
        elif c == "status":
            self._show_status(loop, tui)
        elif c == "plan":
            console.dim("计划详情见上方拆分输出")
        elif c in ("new", "restart", "resume", "continue"):
            console.dim("当前任务仍在执行；请等待结束后在 tasker> 提示符下使用 /new、/restart 或 /continue")
        else:
            console.dim(f"未知指令 :{c}，输入 :help 查看")

    def _show_final(self, session: Session) -> None:
        console.banner("结果")
        graph = self.store.load_plan(session.session_id)
        print("子任务执行汇总:")
        self._print_task_rows(self._session_status_rows(session, graph), include_output=True)
        print()
        if session.status == "goal_achieved":
            console.ok(f"目标已达成（第 {session.iteration} 轮）")
            summary = session.state.get("last_summary", "")
            if summary:
                print(summary[:2000])
        elif session.status == "paused":
            console.warn(f"已达 {session.iteration} 轮仍未达成，回到 REPL")
            console.dim(
                "/continue 继续未完成任务 | /restart <task_id> 从子任务重启 | /resume "
                + session.session_id
                + " 恢复 | /new <目标> 新会话"
            )
        else:
            console.dim(f"会话状态: {session.status}")
        print(
            f"会话 id: {session.session_id}（/continue 继续；/restart <task_id> 重启子任务；"
            f"/new <目标> 新建会话）"
        )


def main_loop(cfg: Config, *, template=None) -> int:
    return Repl(cfg, template=template).run()
