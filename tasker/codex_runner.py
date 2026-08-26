
from __future__ import annotations

import json
import threading
import time

from .approvals import ApprovalBroker
from .config import Config
from .models import Event, TaskRun
from .spawn import resolve_binary, start_process

_REASON_FIELDS = ("reasoning", "reasoning_content", "thinking", "chain_of_thought")


class CodexRunner:
    source = "codex"
    self_handles_approval = True

    def __init__(self, cfg: Config, run: TaskRun, workdir: str, on_event, prompt: str, broker=None):
        self.cfg = cfg
        self.run = run
        self.workdir = workdir
        self.on_event = on_event
        self.prompt = prompt
        self.broker = broker or ApprovalBroker(cfg.approval)
        self.channel = None
        self._tool_names: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interactions: list[str] = []

    def build_args(self) -> list[str]:
        c = self.cfg.codex
        args = [resolve_binary(c.binary), "exec", "--json"]
        if c.full_trace:
            args.append("--full-trace")
        if c.skip_git_check:
            args.append("--skip-git-repo-check")
        if c.model:
            args += ["--model", c.model]
        if c.auto_approve:
            args.append("--auto-approve")
        if c.sandbox:
            args += ["--sandbox", c.sandbox]
        args += c.extra_args
        return args

    def start(self):
        self.run.started_at = time.time()
        args = self.build_args()
        self.channel = start_process(args + [self.prompt], workdir=self.workdir, name=f"codex-{self.run.task.id}")
        self._thread = threading.Thread(target=self._pump, daemon=True, name=f"codex-{self.run.task.id}")
        self._thread.start()
        return self.channel

    def send_message(self, text: str) -> bool:
        """codex exec 不支持中途注入；记录并返回 False（调用方会提示）。"""
        self._interactions.append(text)
        self._emit(Event(kind="interaction", source=self.source, text=f"[无法中途注入] 已排队待下一轮：{text}"))
        return False

    def queued_messages(self) -> list[str]:
        return list(self._interactions)

    @property
    def pending_approval_ids(self) -> list[str]:
        return self.broker.pending_ids

    def approval_respond(self, req_id: str, allowed: bool) -> bool:
        return self.broker.resolve(req_id, allowed=allowed)

    def stop(self) -> None:
        self._stop.set()
        if self.channel:
            self.channel.stop()

    def is_alive(self) -> bool:
        return bool(self.channel and self.channel.is_alive())

    def is_done(self) -> bool:
        return not self.is_alive()

    def finalize(self) -> None:
        pass

    def _emit(self, event: Event) -> None:
        self.run.events.append(event)
        self.on_event(self.run, event)

    def _emit_decision(self, req_id: str, allowed: bool, note: str = "") -> None:
        head = "批准" if allowed else "拒绝"
        self._emit(
            Event(
                kind="permission_result",
                source=self.source,
                text=f"{head} {note} 请求 {req_id}".strip(),
                data={"allowed": allowed, "id": req_id},
            )
        )

    def _pump(self) -> None:
        ch = self.channel
        if not ch:
            return
        try:
            while not self._stop.is_set():
                line = ch.next_line(timeout=0.2)
                if line is None:
                    if not ch.is_alive():
                        break
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    self._emit(Event(kind="raw", source=self.source, text=line[:2000]))
                    continue
                self._handle(data)
        finally:
            self.run.ended_at = time.time()

    def _handle(self, data: dict) -> None:
        t = data.get("type")
        if t == "message":
            for block in data.get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype in _REASON_FIELDS:
                    self._emit(
                        Event(
                            kind="thinking",
                            source=self.source,
                            text=str(block.get(btype, "")),
                            data={"block_type": btype},
                        )
                    )
                elif btype == "text":
                    self._emit(Event(kind="text", source=self.source, text=str(block.get("text", ""))))
                elif btype == "tool_call":
                    self._tool_names[str(block.get("id", ""))] = str(block.get("name", ""))
                    self._emit(
                        Event(
                            kind="tool_use",
                            source=self.source,
                            text=str(block.get("name", "?")),
                            data={"tool": block.get("name"), "input": block.get("input", {}), "id": block.get("id")},
                        )
                    )
        elif t == "tool_call":
            name = str(data.get("tool_name") or data.get("name") or "?")
            tid = str(data.get("id") or "")
            if tid:
                self._tool_names[tid] = name
            self._emit(
                Event(
                    kind="tool_use",
                    source=self.source,
                    text=name,
                    data={"tool": name, "input": data.get("input", {}), "id": data.get("id")},
                )
            )
        elif t == "tool_call_output":
            tid = str(data.get("id") or "")
            name = self._tool_names.get(tid, "?")
            out = data.get("output", "")
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False, default=str)
            self._emit(
                Event(
                    kind="tool_result",
                    source=self.source,
                    text=out,
                    data={"tool": name, "is_error": bool(data.get("is_error")), "id": tid},
                )
            )
        elif t == "approval_request":
            tc = data.get("tool_call") or {}
            req_id = str(data.get("id") or f"codex-{time.time()}")
            req = Event(
                kind="permission_request",
                source=self.source,
                text=str(tc.get("name", "?")),
                data={
                    "id": req_id,
                    "tool": tc.get("name"),
                    "input": tc.get("input"),
                    "sandbox": data.get("sandbox"),
                    "request_data": data,
                },
            )
            self._emit(req)

            mode = self.cfg.approval.mode
            if mode == "auto":
                allowed = self.cfg.approval.default_allow
                self._emit_decision(req_id, allowed, "auto 模式")
            elif mode == "log":
                self._emit_decision(req_id, False, "log 模式：仅记录，默认拒绝")
            else:  # ask_console —— 阻塞 pump 等待 :allow/:deny
                got, allowed, _feedback = self.broker.wait_decision(req_id, kind="permission", run=self.run, event=req)
                if got and allowed is not None:
                    self._emit_decision(req_id, allowed)
                else:
                    self._emit_decision(req_id, False, "审批超时，默认拒绝")
        elif t == "completed":
            self.run.output = str(data.get("result", "") or data.get("response", ""))
            self.run.exit_code = 0 if not data.get("is_error") else (data.get("error") or 1)
            if self.run.exit_code == 0:
                self.run.status = "success"
            self._emit(
                Event(
                    kind="result",
                    source=self.source,
                    text=self.run.output,
                    data={"cost": data.get("cost"), "total_cost_usd": data.get("total_cost_usd"), "raw": data},
                )
            )
        else:
            self._emit(Event(kind="raw", source=self.source, text=json.dumps(data, ensure_ascii=False)[:2000]))
