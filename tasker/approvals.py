
from __future__ import annotations

import threading
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
