"""审批请求处理策略。

headless 模式下各 CLI 对审批的表现不同：
- claude -p：权限弹窗只在交互 TTY 出现；headless 下工具按 permission_mode 自动允许/拒绝，
  我们把拒绝事件（tool_result / result.permission_denials）采集上报，必要时回注一条用户消息
  告知模型"用户已批准，请继续"。
- codex exec --json：会在 stdout 发 approval_request 事件，但非交互下无法经 stdin 回答；
  auto_approve 由 --auto-approve 开关控制。
真正的"人点批准/拒绝"交互请用 ptty attach 模式（macOS 原生支持）。
"""
from __future__ import annotations

import time

from .config import ApprovalConfig
from .models import Event, TaskRun

MODES = ("auto", "log", "ask_console")


class ApprovalPolicy:
    def __init__(self, cfg: ApprovalConfig, on_decision=None):
        self.cfg = cfg
        self.on_decision = on_decision
        self.pending: dict[str, dict] = {}  # id -> {"run": TaskRun, "event": Event}

    def handle(self, run: TaskRun, event: Event, emit, send_msg):
        """处理一条 permission_request 事件。emit: (run, Event) 回调；send_msg: 回注消息函数。"""
        req_id = str(event.data.get("id") or event.data.get("tool_use_id") or time.time())
        mode = self.cfg.mode
        self.pending[req_id] = {"run": run, "event": event, "ts": time.time()}

        if mode == "auto":
            allowed = self.cfg.default_allow
            self._decide(req_id, allowed, run, event, emit, send_msg)
        elif mode == "ask_console":
            # 在 CLI 里已实时展示，等待用户 :allow / :deny
            return
        else:  # log
            return

    def decide(self, req_id: str, allowed: bool, emit, send_msg) -> bool:
        """由用户指令 :allow/:deny 触发。"""
        item = self.pending.get(req_id)
        if item is None:
            # 没有登记的请求：允许对所有运行中 runner 注入"用户决定"消息
            return False
        self._decide(req_id, allowed, item["run"], item["event"], emit, send_msg)
        return True

    def _decide(self, req_id, allowed, run, event, emit, send_msg) -> None:
        note = "批准" if allowed else "拒绝"
        emit(
            run,
            Event(
                kind="permission_result",
                source=run.task.executor,
                text=f"{note}请求 {req_id}",
                data={"allowed": allowed, "id": req_id},
            ),
        )
        if send_msg:
            guidance = "用户已批准该工具请求，如适用请继续执行。" if allowed else "用户已拒绝该请求，请换一种安全方式继续。"
            send_msg(f"[审批反馈] {guidance}（{event.text}）")
        self.pending.pop(req_id, None)
