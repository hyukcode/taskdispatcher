
from __future__ import annotations

import threading
import time

from . import console
from .approvals import ApprovalBroker
from .config import Config
from .goal_loop import GoalLoop
from .live import HELP, LiveTui
from .models import Session
from .session import SessionStore, new_session_id

REPL_HELP = """\
命令（Idle 提示符 tasker> 下）：

  <目标>                输入目标，开始编排执行
  /plan <目标>          只拆分计划，不执行
  /sessions             列出历史会话
  /resume <session_id>  续跑某个会话（继续 goal loop）
  /continue             对当前会话再跑一轮
  /replan               清空状态重新拆分当前目标
  /config               显示当前配置摘要
  /help                 显示本帮助
  /quit                 退出（也可直接输入 quit / q / exit，或 Ctrl+C / Ctrl+D）
"""


class Repl:
    def __init__(self, cfg: Config, *, planner=None, evaluator=None, judge=None, template=None):
        self.cfg = cfg
        self.store = SessionStore(cfg.session)
        self.broker = ApprovalBroker(cfg.approval)
        self.session: Session | None = None
        self.planner = planner
        self.evaluator = evaluator
        self.judge = judge
        self.template = template
        self._quit_requested = False  

    def run(self) -> int:
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

    def _idle_command(self, line: str) -> bool:
        parts = line[1:].split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("quit", "q", "exit"):
            return False
        if cmd == "help":
            print(REPL_HELP)
        elif cmd == "sessions":
            self._list_sessions()
        elif cmd == "resume":
            self._resume(arg)
        elif cmd == "continue":
            self._continue()
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
        console.info(f"恢复会话 {session_id}（goal: {session.goal}，iter={session.iteration}）")
        self._run_goal(session.goal, session)

    def _continue(self) -> None:
        if self.session is None:
            console.warn("当前没有活动会话，先输入目标或 /resume")
            return
        self._run_goal(self.session.goal, self.session)

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
            plan = plan_with_rules(goal, template=self.template) if self.cfg.mock else plan_with_llm(
                goal, self.cfg, emit=lambda e: None, template=self.template
            )
        except LLMError as e:
            console.warn(f"拆分 LLM 不可用（{e}），用规则拆分")
            plan = plan_with_rules(goal, template=self.template)
        print(f"目标: {plan.objective}")
        if plan.template:
            print(f"模板: {plan.template}")
        for t in plan.tasks:
            print(f"  {t.id} [{t.executor}] ← {','.join(t.depends_on) or '—'}  {t.title}")

    def _show_config(self) -> None:
        c = self.cfg
        print(f"display.level={c.display.level}   dispatch.min_multiagent_steps={c.dispatch.min_multiagent_steps}")
        print(f"goal_loop.max_iterations={c.goal_loop.max_iterations}   evaluator={c.goal_loop.evaluator}")
        print(f"approval.mode={c.approval.mode}   mock={c.mock}")
        print(f"session.dir={c.session.path}")

    def _run_goal(self, goal: str, session: Session | None = None) -> None:
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
            planner=self.planner, evaluator=self.evaluator, judge=self.judge,
            template=self.template,
        )

        def worker() -> None:
            loop.run_goal(goal, session)

        th = threading.Thread(target=worker, daemon=True, name="goal-loop")
        th.start()

        tui.start()
        try:
            while th.is_alive():
                for cmd in tui.poll_commands():
                    self._handle_run_command(cmd, loop, tui)
                time.sleep(0.1)
        finally:
            tui.stop()
        th.join()

        self._show_final(session)

    def _handle_run_command(self, cmd: dict, loop: GoalLoop, tui: LiveTui) -> None:
        ctype = cmd["type"]
        if ctype == "noop":
            return
        if ctype == "msg":
            matched = loop.send_message(cmd["target"], cmd["text"])
            if not matched:
                console.warn("没有匹配的活跃 agent（或已结束）")
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
        elif c == "reject":
            req_id = self.broker.find_pending_id(kind="review")
            if not req_id:
                console.warn("没有待处理的人工审查点")
                return
            self.broker.resolve(req_id, allowed=False, feedback=arg)
        elif c in ("quit", "q", "exit"):
            console.warn("终止执行并退出…")
            self._quit_requested = True
            loop.stop()
        elif c == "help":
            tui.print_raw(HELP)
        elif c == "status":
            console.dim("运行中（minimal 显示下详情见 session 事件日志）")
        elif c == "plan":
            console.dim("计划详情见上方拆分输出")
        else:
            console.dim(f"未知指令 :{c}，输入 :help 查看")

    def _show_final(self, session: Session) -> None:
        console.banner("结果")
        if session.status == "goal_achieved":
            console.ok(f"目标已达成（第 {session.iteration} 轮）")
            summary = session.state.get("last_summary", "")
            if summary:
                print(summary[:2000])
        elif session.status == "paused":
            console.warn(f"已达 {session.iteration} 轮仍未达成，回到 REPL")
            console.dim("/continue 再跑一轮 | /replan 重新拆分 | 输入新目标重新开始")
        else:
            console.dim(f"会话状态: {session.status}")
        print(f"会话 id: {session.session_id}（/resume {session.session_id} 可续跑）")


def main_loop(cfg: Config, *, template=None) -> int:
    return Repl(cfg, template=template).run()
