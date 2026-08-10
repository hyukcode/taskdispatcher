"""Codex App-Server 采集器（JSON-RPC over stdio，替代 codex exec）。

与 CodexRunner（codex exec --json 单次执行）相比：
- app-server 是持久 JSON-RPC 2.0 进程，支持双向通信。
- 关键升级：审批请求是 server→client 的 JSON-RPC request（带 id），
  编排器可以程序化批准/拒绝，实现和 SdkClaudeRunner.can_use_tool 对等的
  headless 审批能力。
- 中途注入：turn/steer 可在 turn 进行中追加用户消息。

传输：codex app-server --listen stdio://
协议：newline-delimited JSON（无 "jsonrpc":"2.0" 头），camelCase 字段。
"""

from __future__ import annotations

import json
import threading
import time

from . import __version__
from .config import Config
from .models import Event, TaskRun
from .spawn import ProcChannel, resolve_binary, start_process

# ---- 审批相关的 server→client 请求方法 ----
_APPROVAL_METHODS = frozenset(
    (
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "mcpServer/elicitation/request",
        "item/tool/requestUserInput",
    )
)

# item 类型 → tool_use 展示名（item/started 时用）
_ITEM_TOOL_MAP: dict[str, str] = {
    "commandExecution": "run_command",
    "fileChange": "edit_file",
    "mcpToolCall": "mcp_tool_call",
    "webSearch": "web_search",
    "imageGeneration": "image_generation",
    "memoryRead": "memory_read",
    "memoryWrite": "memory_write",
}


class CodexAppServerRunner:
    """与 CodexRunner 相同的对外接口（start/is_done/finalize/stop/send_message/is_alive），
    额外暴露 approval_respond/pending_approval_ids 供调度器把 :allow/:deny 送达。
    """

    source = "codex"
    self_handles_approval = True

    def __init__(self, cfg: Config, run: TaskRun, workdir: str, on_event, prompt: str):
        self.cfg = cfg
        self.run = run
        self.workdir = workdir
        self.on_event = on_event
        self.prompt = prompt
        self.channel: ProcChannel | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rpc_id = 0
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._turn_status: str | None = None
        self._handshake_ok = False
        self._closed = False
        self._tool_names: dict[str, str] = {}
        self._buffers: dict[str, dict] = {}  # itemId → {"kind":"text"|"thinking","parts":[str]}
        self._interactions: list[str] = []
        self._result_ts: float | None = None
        self._injection_ts: float | None = None
        self._last_event_ts = time.time()
        self._settle = float(getattr(cfg.codex, "completion_idle", 5.0))
        self._pending_approvals: dict[str, tuple] = {}  # rpc_id → (msg, threading.Event, holder)
        self._steer_queue: list[str] = []
        self._steer_lock = threading.Lock()

    # ---------- 生命周期 ----------
    def build_args(self) -> list[str]:
        c = self.cfg.codex
        # app-server 只接受 --listen/--config/--enable/--disable 等，不接受 exec 专用 flag
        args = [resolve_binary(c.binary), "app-server", "--listen", "stdio://"]
        args += c.extra_args
        return args

    def start(self) -> ProcChannel | None:
        self.run.started_at = time.time()
        self.channel = start_process(self.build_args(), workdir=self.workdir, name=f"codex-as-{self.run.task.id}")
        self._thread = threading.Thread(target=self._pump, daemon=True, name=f"codex-as-{self.run.task.id}")
        self._thread.start()
        return self.channel

    # ---------- 完成判定（同 sdk_runner 的 settle 逻辑）----------
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def is_done(self) -> bool:
        if self._closed or not self.is_alive():
            return True
        if self._result_ts is None:
            return False
        ref = max(self._result_ts, self._injection_ts or 0)
        now = time.time()
        return (now - ref) > self._settle and (now - self._last_event_ts) > self._settle

    def finalize(self) -> None:
        self._stop.set()
        if self.channel and self.channel.is_alive():
            self.channel.close_stdin()
            try:
                self.channel.proc.wait(timeout=8)
            except Exception:
                self.channel.stop()

    def stop(self) -> None:
        self._stop.set()
        if self.channel:
            self.channel.stop()

    # ---------- 中途注入 ----------
    def send_message(self, text: str) -> bool:
        if self._stop.is_set() or not self.channel or not self.channel.is_alive():
            return False
        self._interactions.append(text)
        self._injection_ts = time.time()
        self._emit(Event(kind="user_message", source=self.source, text=text))
        with self._steer_lock:
            if not self._handshake_ok or not self._turn_id:
                self._steer_queue.append(text)
                return True
        rid = self._next_id()
        ok = self._write(
            {
                "id": rid,
                "method": "turn/steer",
                "params": {
                    "threadId": self._thread_id,
                    "turnId": self._turn_id,
                    "input": [{"type": "text", "text": text}],
                    "expectedTurnId": self._turn_id,
                },
            }
        )
        return ok

    # ---------- 审批接口（duck-typed，供 scheduler 的 :allow/:deny 路由）----------
    @property
    def pending_approval_ids(self) -> list[str]:
        return list(self._pending_approvals.keys())

    def approval_respond(self, req_id: str, allowed: bool) -> bool:
        item = self._pending_approvals.get(req_id)
        if item is None:
            return False
        _msg, ev, holder = item
        holder["allowed"] = allowed
        try:
            ev.set()
            return True
        except Exception:  # noqa: BLE001
            return False

    # ================================================================
    #  事件泵
    # ================================================================
    def _emit(self, event: Event) -> None:
        self._last_event_ts = time.time()
        self.run.events.append(event)
        self.on_event(self.run, event)

    def _pump(self) -> None:
        ch = self.channel
        if not ch:
            return
        try:
            self._handshake(ch)
            if not self._handshake_ok:
                return
            # 补发握手中排队的 steer 消息
            with self._steer_lock:
                for text in self._steer_queue:
                    self._send_steer(text)
                self._steer_queue.clear()

            while not self._stop.is_set():
                line = ch.next_line(timeout=0.2)
                if line is None:
                    if not ch.is_alive():
                        break
                    continue
                self._last_event_ts = time.time()
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self._emit(Event(kind="raw", source=self.source, text=line[:2000]))
                    continue
                try:
                    self._dispatch(msg)
                except Exception as e:  # noqa: BLE001
                    self._emit(Event(kind="error", source=self.source, text=f"消息处理失败: {e}"))
        finally:
            self.run.ended_at = time.time()

    # ================================================================
    #  握手
    # ================================================================
    def _handshake(self, ch: ProcChannel) -> None:
        # 1) initialize
        rid = self._next_id()
        self._write(
            {
                "id": rid,
                "method": "initialize",
                "params": {"clientInfo": {"name": "tasker", "version": __version__}},
            }
        )
        resp = self._await_response(ch, rid, timeout=15)
        if resp is None or "error" in resp:
            self.run.exit_code = 1
            err = (resp or {}).get("error", {}).get("message", "无响应")
            self._emit(Event(kind="error", source=self.source, text=f"app-server 握手失败 (initialize): {err}"))
            return

        # 2) initialized
        self._write({"method": "initialized"})

        # 3) thread/start
        c = self.cfg.codex
        rid = self._next_id()
        self._write(
            {
                "id": rid,
                "method": "thread/start",
                "params": {
                    "cwd": self.workdir,
                    "sandbox": c.sandbox,
                    "approvalPolicy": c.approval_policy,
                    **({"model": c.model} if c.model else {}),
                },
            }
        )
        resp = self._await_response(ch, rid, timeout=30)
        if resp is None or "error" in resp:
            self.run.exit_code = 1
            err = (resp or {}).get("error", {}).get("message", "无响应")
            self._emit(Event(kind="error", source=self.source, text=f"thread/start 失败: {err}"))
            return

        result = resp.get("result", {})
        thread = result.get("thread", result)
        self._thread_id = thread.get("id")
        if not self._thread_id:
            self.run.exit_code = 1
            self._emit(Event(kind="error", source=self.source, text="thread/start 未返回 thread.id"))
            return

        # 4) turn/start
        rid = self._next_id()
        self._write(
            {
                "id": rid,
                "method": "turn/start",
                "params": {
                    "threadId": self._thread_id,
                    "input": [{"type": "text", "text": self.prompt}],
                    "cwd": self.workdir,
                    "approvalPolicy": c.approval_policy,
                    **({"model": c.model} if c.model else {}),
                },
            }
        )
        resp = self._await_response(ch, rid, timeout=30)
        if resp is None or "error" in resp:
            self.run.exit_code = 1
            err = (resp or {}).get("error", {}).get("message", "无响应")
            self._emit(Event(kind="error", source=self.source, text=f"turn/start 失败: {err}"))
            return

        result = resp.get("result", {})
        turn = result.get("turn", result)
        self._turn_id = turn.get("id")
        if not self._turn_id:
            self.run.exit_code = 1
            self._emit(Event(kind="error", source=self.source, text="turn/start 未返回 turn.id"))
            return

        self._handshake_ok = True
        self.run.exit_code = 0
        self._emit(
            Event(
                kind="system",
                source=self.source,
                text=f"会话初始化（app-server） thread={self._thread_id[:8]}… turn={self._turn_id[:8]}…",
                data={"thread_id": self._thread_id, "turn_id": self._turn_id},
            )
        )

    def _await_response(self, ch: ProcChannel, rpc_id: int, timeout: float) -> dict | None:
        """等待指定 id 的响应，期间的通知照常采集。"""
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop.is_set():
            line = ch.next_line(timeout=0.2)
            if line is None:
                if not ch.is_alive():
                    return None
                continue
            self._last_event_ts = time.time()
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._emit(Event(kind="raw", source=self.source, text=line[:2000]))
                continue
            if "id" in msg and "method" not in msg:
                if msg.get("id") == rpc_id:
                    return msg
                # 其他响应（如延迟到达的旧请求）忽略
                continue
            if "method" in msg:
                self._dispatch_notification(msg)
        return None

    # ================================================================
    #  分发
    # ================================================================
    def _dispatch(self, msg: dict) -> None:
        """主循环分发：三类帧 → response / server request / notification。"""
        has_id = "id" in msg
        has_method = "method" in msg

        if has_id and not has_method:
            if "error" in msg:
                self._emit(
                    Event(
                        kind="error",
                        source=self.source,
                        text=f"RPC error: {_compact(msg['error'], 300)}",
                    )
                )
            return  # ack / 响应（send_message 的 turn/steer 响应等，忽略）

        if has_method:
            if has_id:
                self._handle_server_request(msg)
            else:
                self._dispatch_notification(msg)
            return

        self._emit(Event(kind="raw", source=self.source, text=json.dumps(msg, ensure_ascii=False)[:2000]))

    def _dispatch_notification(self, msg: dict) -> None:
        """处理 server→client 通知（无 id，不需要响应）。"""
        method = msg.get("method", "")
        params = msg.get("params") or {}

        if method == "thread/started":
            thread = params.get("thread", params)
            self._thread_id = self._thread_id or thread.get("id")
            self._emit(
                Event(
                    kind="system",
                    source=self.source,
                    text=f"thread/started id={thread.get('id','')[:8]}…",
                    data=thread,
                )
            )
        elif method == "turn/started":
            turn = params.get("turn", params)
            self._turn_id = self._turn_id or turn.get("id")
            self._emit(
                Event(
                    kind="system",
                    source=self.source,
                    text=f"turn/started id={turn.get('id','')[:8]}… status={turn.get('status','')}",
                    data=turn,
                )
            )
        elif method == "turn/completed":
            self._handle_turn_completed(params)
        elif method == "thread/closed":
            self._closed = True
            if self._result_ts is None:
                self._result_ts = time.time()
            self._emit(Event(kind="system", source=self.source, text="thread/closed", data=params))
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or params
            total = usage.get("total", usage)
            cost = float(total.get("total_cost_usd") or 0.0)
            if cost:
                self.run.cost_usd = cost
        elif method == "thread/environment/connected":
            self._emit(
                Event(
                    kind="system",
                    source=self.source,
                    text=f"environment/connected {_compact(params, 200)}",
                    data=params,
                )
            )
        elif method == "item/started":
            self._handle_item_started(params)
        elif method == "item/agentMessage/delta":
            self._buffer_delta(params.get("itemId", ""), "text", params.get("delta", ""))
        elif method == "item/reasoning/textDelta":
            self._buffer_delta(params.get("itemId", ""), "thinking", params.get("delta", ""))
        elif method == "item/reasoning/summaryTextDelta":
            self._buffer_delta(params.get("itemId", ""), "thinking", params.get("delta", ""))
        elif method == "item/reasoning/summaryPartAdded":
            self._buffer_delta(params.get("itemId", ""), "thinking", params.get("summary", ""))
        elif method == "item/completed":
            self._handle_item_completed(params)
        elif method == "item/commandExecution/outputDelta":
            pass  # 工具 stdout 流，不 flood 事件流（最终 tool_result 里有 output）
        elif method == "turn/diff/updated":
            pass  # diff 快照，用于 UI；编排器暂不需要
        elif method == "serverRequest/resolved":
            pass  # 审批已解决，确认信号
        elif method == "error":
            err = params.get("error", params)
            self._emit(
                Event(
                    kind="error",
                    source=self.source,
                    text=str(err.get("message", "") or _compact(err, 200)),
                    data=params,
                )
            )
        elif method == "warning" or method == "configWarning":
            self._emit(
                Event(
                    kind="system",
                    source=self.source,
                    text=f"{method}: {params.get('message', params.get('summary', ''))}",
                    data=params,
                )
            )
        elif method == "model/rerouted":
            self._emit(
                Event(
                    kind="system",
                    source=self.source,
                    text=f"模型重路由: {_compact(params, 200)}",
                    data=params,
                )
            )
        elif method in ("remoteControl/status/changed", "account/rateLimits/updated",
                        "mcpServer/startupStatus/updated"):
            pass  # 高频噪音，忽略
        elif method == "thread/status/changed":
            pass  # thread 状态变更，对编排器无意义
        else:
            self._emit(
                Event(
                    kind="raw",
                    source=self.source,
                    text=json.dumps({"method": method, "params": params}, ensure_ascii=False)[:2000],
                )
            )

    # ================================================================
    #  item 生命周期
    # ================================================================
    def _handle_item_started(self, params: dict) -> None:
        item = params.get("item", params)
        item_id = item.get("id", "")
        item_type = item.get("type", "")

        # 记录 itemId → 工具名映射
        tool_name = _ITEM_TOOL_MAP.get(item_type, item_type)
        if item_type not in ("agentMessage", "reasoning", "userMessage", "plan"):
            self._tool_names[item_id] = tool_name
            self._emit(
                Event(
                    kind="tool_use",
                    source=self.source,
                    text=tool_name,
                    data={
                        "tool": tool_name,
                        "input": item,
                        "id": item_id,
                        "item_type": item_type,
                    },
                )
            )

    def _buffer_delta(self, item_id: str, kind: str, delta: str) -> None:
        if not delta:
            return
        buf = self._buffers.get(item_id)
        if buf is None:
            buf = {"kind": kind, "parts": []}
            self._buffers[item_id] = buf
        buf["parts"].append(delta)

    def _handle_item_completed(self, params: dict) -> None:
        item = params.get("item", params)
        item_id = item.get("id", "")
        item_type = item.get("type", "")

        # flush 缓冲的 delta → text/thinking 事件
        buf = self._buffers.pop(item_id, None)
        if buf and buf["parts"]:
            text = "".join(buf["parts"])
            kind = "text" if buf["kind"] == "text" else "thinking"
            self._emit(Event(kind=kind, source=self.source, text=text, data={"item_id": item_id}))

        # tool 类 item：发 tool_result
        if item_type in _ITEM_TOOL_MAP or item_type in (
            "commandExecution",
            "fileChange",
            "mcpToolCall",
            "webSearch",
        ):
            name = self._tool_names.get(item_id, item_type)
            status = item.get("status", "")
            is_error = status in ("failed", "declined", "cancelled", "error")
            output = item.get("text") or item.get("aggregatedOutput") or ""
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False, default=str)
            self._emit(
                Event(
                    kind="tool_result",
                    source=self.source,
                    text=output,
                    data={
                        "tool": name,
                        "is_error": is_error,
                        "id": item_id,
                        "item_type": item_type,
                        "status": status,
                    },
                )
            )
        # agentMessage 完整文本（如果没有 delta 过，这里拿全文）
        elif item_type == "agentMessage" and not (buf and buf["parts"]):
            text = item.get("text", "")
            if text:
                self._emit(Event(kind="text", source=self.source, text=str(text), data={"item_id": item_id}))
        elif item_type == "reasoning" and not (buf and buf["parts"]):
            text = item.get("text", "")
            if text:
                self._emit(Event(kind="thinking", source=self.source, text=str(text), data={"item_id": item_id}))

    def _handle_turn_completed(self, params: dict) -> None:
        turn = params.get("turn", params)
        status = turn.get("status", "completed")
        self._turn_status = status
        self._result_ts = time.time()

        # flush 剩余缓冲
        for item_id, buf in list(self._buffers.items()):
            if buf["parts"]:
                kind = "text" if buf["kind"] == "text" else "thinking"
                self._emit(Event(kind=kind, source=self.source, text="".join(buf["parts"]), data={"item_id": item_id}))
        self._buffers.clear()

        if status == "failed":
            error_info = turn.get("error", {})
            self.run.exit_code = 1
            self.run.status = "failed"
            self.run.output = str(error_info.get("message", "") or _compact(error_info, 500))
            self._emit(
                Event(
                    kind="error",
                    source=self.source,
                    text=f"turn failed: {self.run.output}",
                    data={"status": status, "error": error_info},
                )
            )
            self._emit(
                Event(
                    kind="result",
                    source=self.source,
                    text=self.run.output,
                    data={"status": status, "cost_usd": self.run.cost_usd},
                )
            )
            return

        # completed / interrupted
        self.run.exit_code = 0
        self.run.status = "success"
        # 从最后一个 agentMessage item 提取输出
        items = turn.get("items") or []
        output_parts: list[str] = []
        for item in reversed(items):
            if isinstance(item, dict):
                if item.get("type") == "agentMessage":
                    output_parts.append(str(item.get("text", "")))
                    break
        if not output_parts:
            # fallback：从 buffered text 中取
            output_parts.append("")
        self.run.output = output_parts[0] if output_parts else ""

        self._emit(
            Event(
                kind="result",
                source=self.source,
                text=self.run.output,
                data={
                    "status": status,
                    "cost_usd": self.run.cost_usd,
                    "turn_id": self._turn_id,
                },
            )
        )

    # ================================================================
    #  审批处理（self-handled）
    # ================================================================
    def _handle_server_request(self, msg: dict) -> None:
        method = msg.get("method", "")
        if method in _APPROVAL_METHODS:
            self._handle_approval(msg)
        else:
            self._respond(msg["id"], error={"code": -32601, "message": f"Method not found: {method}"})
            self._emit(
                Event(
                    kind="raw",
                    source=self.source,
                    text=f"[未处理请求] {method} {_compact(msg.get('params', {}), 200)}",
                )
            )

    def _handle_approval(self, msg: dict) -> None:
        method = msg.get("method", "")
        params = msg.get("params") or {}
        rid = str(msg.get("id"))

        # 构建 permission_request 展示文本
        if method == "item/permissions/requestApproval":
            perm = params.get("permissions", {})
            text = f"权限审批: {_compact(perm, 160)}"
            data = {"id": rid, "tool": "permissions", "input": perm, "request_data": params}
        elif method == "mcpServer/elicitation/request":
            text = f"MCP elicitation: {params.get('serverName','?')} {params.get('message','')[:100]}"
            data = {"id": rid, "tool": "mcp_elicitation", "input": params, "request_data": params}
        elif method == "item/tool/requestUserInput":
            text = f"工具请求输入: {_compact(params, 160)}"
            data = {"id": rid, "tool": "request_user_input", "input": params, "request_data": params}
        else:
            # commandExecution / fileChange
            display = params.get("command") or params.get("reason") or _compact(params, 120)
            text = str(display)[:200]
            data = {"id": rid, "tool": method.split("/")[1], "input": params, "request_data": params}

        req = Event(kind="permission_request", source=self.source, text=text, data=data)
        self._emit(req)

        mode = self.cfg.approval.mode
        if mode == "auto":
            self._decide_approval(msg, bool(self.cfg.approval.default_allow), "auto 模式")
        elif mode == "log":
            self._decide_approval(msg, False, "log 模式：仅记录，默认拒绝")
        else:  # ask_console —— 阻塞 pump 等待 :allow/:deny
            ev = threading.Event()
            holder: dict = {"allowed": None}
            self._pending_approvals[rid] = (msg, ev, holder)
            got = ev.wait(timeout=self.cfg.approval.timeout)
            self._pending_approvals.pop(rid, None)
            if got and holder["allowed"] is not None:
                self._decide_approval(msg, holder["allowed"])
            else:
                self._decide_approval(msg, False, "审批超时，默认拒绝")

    def _decide_approval(self, msg: dict, allowed: bool, note: str = "") -> None:
        rid = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        # 按审批类型构建响应
        if method == "item/permissions/requestApproval":
            self._respond(
                rid,
                result={
                    "permissions": params.get("permissions") if allowed else {},
                    "scope": params.get("scope", "turn"),
                },
            )
        elif method == "mcpServer/elicitation/request":
            self._respond(
                rid,
                result={
                    "action": "accept" if allowed else "decline",
                    "content": "",
                },
            )
        elif method == "item/tool/requestUserInput":
            self._respond(rid, result={"answers": {}})  # 默认空答案
        else:
            # commandExecution / fileChange
            self._respond(rid, result={"decision": "accept" if allowed else "decline"})

        self._emit(
            Event(
                kind="permission_result",
                source=self.source,
                text=f"{'批准' if allowed else '拒绝'} {note} 请求 {rid}".strip(),
                data={"allowed": allowed, "id": str(rid)},
            )
        )

    # ================================================================
    #  传输辅助
    # ================================================================
    def _write(self, payload: dict) -> bool:
        if not self.channel or not self.channel.is_alive():
            return False
        return self.channel.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _respond(self, rpc_id, result: dict | None = None, error: dict | None = None) -> None:
        payload: dict = {"id": rpc_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result or {}
        self._write(payload)

    def _send_steer(self, text: str) -> None:
        """在 pump 线程内调用；调用前需持有 _steer_lock。"""
        if not self._thread_id or not self._turn_id:
            return
        rid = self._next_id()
        self._write(
            {
                "id": rid,
                "method": "turn/steer",
                "params": {
                    "threadId": self._thread_id,
                    "turnId": self._turn_id,
                    "input": [{"type": "text", "text": text}],
                    "expectedTurnId": self._turn_id,
                },
            }
        )


def _compact(obj, width: int) -> str:
    """截断 JSON 表示到指定宽度。"""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(obj)
    return s if len(s) <= width else s[: width - 1] + "…"
