"""审批请求处理：统一 Broker + 非自管理策略。

两类「阻塞 flow 线程 + REPL 决定」的交互共用 ApprovalBroker：
1. 工具审批（permission_request）：claude/codex 运行时工具需权限。
2. 人工审查（review_request）：图执行到 executor=="human" 节点。

ApprovalBroker 只负责「待决策注册表 + 解除阻塞」，具体阻塞原语由调用方持有：
- 同步 runner（codex exec / codex app-server）用 wait_decision（threading.Event）。
- 异步 runner（SdkClaudeRunner）用 register_async + resolve（asyncio.Future）。

ApprovalPolicy 保留，用于「非自管理审批」的 runner（mock / claude stream-json）：
headless 下 claude -p 不产生 permission_request，权限按 permission_mode 自动放行/拒绝，
拒绝体现为 tool_result 与 result.permission_denials，一并采集。
"""

from __future__ import annotations

import threading
import time

from .config import ApprovalConfig
from .models import Event, TaskRun

MODES = ("auto", "log", "ask_console")


class ApprovalBroker:
    """统一待决策注册表：审批 + 人工审查，REPL 通过 resolve() 解除阻塞。"""

    def __init__(self, cfg: ApprovalConfig):
        self.cfg = cfg
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---------- 同步阻塞（threading.Event） ----------
    def wait_decision(self, req_id: str, *, kind: str, run: TaskRun | None = None, event: Event | None = None, timeout: float | None = None) -> tuple[bool, bool | None, str]:
        """阻塞当前线程等待决策，返回 (got, allowed, feedback)。"""
        timeout = self.cfg.timeout if timeout is None else timeout
        ev = threading.Event()
        holder: dict = {"allowed": None, "feedback": ""}
        with self._lock:
            self._pending[req_id] = {"kind": kind, "run": run, "event": event, "_ev": ev, "_holder": holder}
        got = ev.wait(timeout=timeout)
        with self._lock:
            self._pending.pop(req_id, None)
        return got, holder["allowed"], holder["feedback"]

    # ---------- 异步阻塞（asyncio.Future，由调用方 resolve） ----------
    def register_async(self, req_id: str, *, kind: str, run: TaskRun | None = None, event: Event | None = None, resolver) -> None:
        with self._lock:
            self._pending[req_id] = {"kind": kind, "run": run, "event": event, "_resolver": resolver}

    def unregister(self, req_id: str) -> None:
        with self._lock:
            self._pending.pop(req_id, None)

    # ---------- 统一解除 ----------
    def resolve(self, req_id: str, *, allowed: bool, feedback: str = "") -> bool:
        with self._lock:
            item = self._pending.pop(req_id, None)
        if item is None:
            return False
        if "_resolver" in item:
            item["_resolver"](allowed, feedback)
            return True
        item["_holder"]["allowed"] = allowed
        item["_holder"]["feedback"] = feedback
        item["_ev"].set()
        return True

    # ---------- 查询 ----------
    def find_pending_id(self, kind: str | None = None) -> str:
        with self._lock:
            items = list(self._pending.items())
        for rid, it in reversed(items):
            if kind is None or it["kind"] == kind:
                return rid
        return ""

    @property
    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._pending.keys())

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)


class ApprovalPolicy:
    """非自管理审批的决策策略（mock / claude stream-json 回退路径）。"""

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
