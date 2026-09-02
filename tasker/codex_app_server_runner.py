"""Codex App-Server 采集器（JSON-RPC over stdio）。

app-server 是持久 JSON-RPC 进程，支持双向通信。
- 关键升级：审批请求是 server→client 的 JSON-RPC request（带 id），
  编排器可以程序化批准/拒绝，实现和 SdkClaudeRunner.can_use_tool 对等的
  headless 审批能力。
- 中途注入：turn/steer 可在 turn 进行中追加用户消息。
- 任务内部 loop：turn/completed 返回 needs_iteration 时，在同一 thread 上
  发起新的 turn/start，不重跑外层任务图。

传输：codex app-server --listen stdio://
协议：newline-delimited JSON（无 "jsonrpc":"2.0" 头），camelCase 字段。
"""

from __future__ import annotations

import json
import logging
import time

from . import __version__
from .config import Config
from .formatting import compact_json as _compact
from .loop_protocol import parse_loop_decision
from .models import Event, TaskRun
from .policy_hooks import HookChain
from .runner_base import EventSink, RunnerBase
from .spawn import ProcChannel, resolve_binary, start_process
from .tool_catalog import ToolCatalog


logger = logging.getLogger(__name__)

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

_LOOP_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["passed", "needs_iteration"]},
        "feedback": {"type": "string"},
    },
    "required": ["status", "feedback"],
    "additionalProperties": False,
}


class CodexAppServerRunner(RunnerBase):
    """提供统一 runner 接口（start/is_done/finalize/stop/send_message/is_alive），
    额外暴露 approval_respond/pending_approval_ids 供 REPL 把 :allow/:deny 送达。
    """

    source = "codex"
    config_key = "codex"

    def __init__(
        self,
        cfg: Config,
        run: TaskRun,
        workdir: str,
        on_event: EventSink,
        prompt: str,
        broker=None,
        tool_catalog: ToolCatalog | None = None,
        hook_chain: HookChain | None = None,
    ):
        super().__init__(
            cfg,
            run,
            workdir,
            on_event,
            prompt,
            broker=broker,
            tool_catalog=tool_catalog,
            hook_chain=hook_chain,
        )
        self.channel: ProcChannel | None = None
        self._rpc_id = 0
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._turn_status: str | None = None
        self._handshake_ok = False
        self._tool_names: dict[str, str] = {}
        self._buffers: dict[str, dict] = {}  # itemId → {"kind":"text"|"thinking","parts":[str]}
        self._turn_active = False
        self._pending_turn_starts: dict[int, bool] = {}
        self._responses: dict[object, dict] = {}
        self._turn_output = ""

    # ---------- 生命周期 ----------
    def build_args(self) -> list[str]:
        c = self.cfg.codex
        # app-server 只接受 --listen/--config/--enable/--disable 等，不接受 exec 专用 flag
        args = [resolve_binary(c.binary), "app-server", "--listen", "stdio://"]
        args += c.extra_args
        return args

    def _prepare_start(self) -> None:
        self.channel = start_process(self.build_args(), workdir=self.workdir, name=f"codex-as-{self.run.task.id}")

    def _start_result(self) -> ProcChannel | None:
        return self.channel

    def _finalize_transport(self) -> None:
        if self.channel and self.channel.is_alive():
            self.channel.close_stdin()
            try:
                self.channel.proc.wait(timeout=8)
            except Exception:
                logger.debug("等待 Codex app-server 结束失败，改为强制停止", exc_info=True)
                self.channel.stop()

    def _stop_transport(self) -> None:
        if self.channel:
            self.channel.stop()

    # ---------- 中途注入 ----------
    def _can_accept_message(self) -> bool:
        return bool(self.channel and self.channel.is_alive())

    def _drain_input_queue(self) -> None:
        """只在 pump 线程中消费，保证 turn 状态和发送操作串行化。"""
        if not self._handshake_ok or not self._thread_id or self._pending_turn_starts:
            return
        while not self._stop.is_set():
            available, text = self._take_input_nowait()
            if not available or text is None:
                return
            if self._turn_active and self._turn_id:
                if not self._send_steer(text):
                    self._finish_failure("无法发送 turn/steer")
                    return
                continue
            # turn/completed 之后必须开启新的 turn，不能再 steer 已结束的 turn。
            self._start_followup_turn(text, internal=False)
            return

    # ================================================================
    #  事件泵
    # ================================================================
    def _run_transport(self) -> None:
        self._pump()

    def _pump(self) -> None:
        ch = self.channel
        if not ch:
            return
        self._handshake(ch)
        if not self._handshake_ok:
            return
        self._drain_input_queue()

        while not self._stop.is_set():
            self._drain_input_queue()
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
            err = (resp or {}).get("error", {}).get("message", "无响应")
            self._finish_failure(f"app-server 握手失败 (initialize): {err}")
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
                    # 当前本机 codex-cli 0.147.0 的 thread/start 仍要求旧的
                    # kebab-case 字符串；turn/start 使用新版 sandboxPolicy 对象。
                    "sandbox": c.sandbox,
                    "approvalPolicy": _approval_policy(c.approval_policy),
                    **({"model": c.model} if c.model else {}),
                },
            }
        )
        resp = self._await_response(ch, rid, timeout=30)
        if resp is None or "error" in resp:
            err = (resp or {}).get("error", {}).get("message", "无响应")
            self._finish_failure(f"thread/start 失败: {err}")
            return

        result = resp.get("result", {})
        thread = result.get("thread", result)
        self._thread_id = thread.get("id")
        if not self._thread_id:
            self._finish_failure("thread/start 未返回 thread.id")
            return

        # 4) turn/start
        self._reset_turn_output()
        rid = self._next_id()
        self._write(
            {
                "id": rid,
                "method": "turn/start",
                "params": {
                    "threadId": self._thread_id,
                    "input": [{"type": "text", "text": self._initial_prompt()}],
                    "cwd": self.workdir,
                    "approvalPolicy": _approval_policy(c.approval_policy),
                    "sandboxPolicy": _sandbox_policy(c.sandbox, self.workdir),
                    **({"model": c.model} if c.model else {}),
                    **({"outputSchema": _LOOP_OUTPUT_SCHEMA} if self._internal_loop else {}),
                },
            }
        )
        resp = self._await_response(ch, rid, timeout=30)
        if resp is None or "error" in resp:
            err = (resp or {}).get("error", {}).get("message", "无响应")
            self._finish_failure(f"turn/start 失败: {err}")
            return

        result = resp.get("result", {})
        turn = result.get("turn", result)
        self._turn_id = turn.get("id")
        if not self._turn_id:
            self._finish_failure("turn/start 未返回 turn.id")
            return

        self._turn_active = True
        self._handshake_ok = True
        self.run.status = "running"
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
        cached = self._responses.pop(rpc_id, None)
        if cached is not None:
            return cached
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
            if "method" in msg:
                if "id" in msg:
                    self._handle_server_request(msg)
                else:
                    self._dispatch_notification(msg)
                continue
            if "id" in msg:
                if msg.get("id") == rpc_id:
                    return msg
                self._responses[msg.get("id")] = msg
                continue
        return None

    # ================================================================
    #  分发
    # ================================================================
    def _dispatch(self, msg: dict) -> None:
        """主循环分发：三类帧 → response / server request / notification。"""
        has_id = "id" in msg
        has_method = "method" in msg

        if has_id and not has_method:
            pending_loop = self._pending_turn_starts.pop(msg.get("id"), None)
            if pending_loop is not None:
                if "error" in msg:
                    self._turn_active = False
                    self._finish_failure(f"turn/start 失败: {_compact(msg['error'], 500)}")
                    return
                result = msg.get("result") or {}
                turn = result.get("turn", result)
                self._turn_id = turn.get("id") or self._turn_id
                self._turn_active = bool(self._turn_id)
                if not self._turn_active:
                    self._finish_failure("follow-up turn/start 未返回 turn.id")
                    return
                self._emit(
                    Event(
                        kind="system",
                        source=self.source,
                        text=f"内部 loop turn={self._turn_id[:8]}… 第 {self._loop_iteration + 1} 轮",
                        data={"thread_id": self._thread_id, "turn_id": self._turn_id, "internal_loop": True},
                    )
                )
                self._drain_input_queue()
                return
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
                    data={**thread, "display": True},
                )
            )
        elif method == "turn/started":
            turn = params.get("turn", params)
            self._turn_id = turn.get("id") or self._turn_id
            self._turn_active = True
            self._emit(
                Event(
                    kind="system",
                    source=self.source,
                    text=f"turn/started id={turn.get('id','')[:8]}… status={turn.get('status','')}",
                    data={**turn, "display": True},
                )
            )
        elif method == "turn/completed":
            self._handle_turn_completed(params)
        elif method == "thread/closed":
            self._closed = True
            if self._result_ts is None:
                self._result_ts = time.time()
            self._emit(Event(kind="system", source=self.source, text="thread/closed", data={**params, "display": True}))
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or params
            total = usage.get("total", usage)
            cost = float(total.get("total_cost_usd") or 0.0)
            if cost:
                self.run.cost_usd = cost
            self._emit(
                Event(
                    kind="usage",
                    source=self.source,
                    text=_usage_text(total),
                    data=dict(total) if isinstance(total, dict) else {"value": total},
                )
            )
        elif method == "thread/environment/connected":
            self._emit(
                Event(
                    kind="system",
                    source=self.source,
                    text=f"environment/connected {_compact(params, 200)}",
                    data={**params, "display": True},
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
        elif method == "item/plan/delta":
            self._buffer_delta(params.get("itemId", ""), "plan", params.get("delta", ""))
        elif method == "item/completed":
            self._handle_item_completed(params)
        elif method == "item/commandExecution/outputDelta":
            pass  # 工具 stdout 流，不 flood 事件流（最终 tool_result 里有 output）
        elif method == "turn/diff/updated":
            self._emit(
                Event(
                    kind="interaction",
                    source=self.source,
                    text=f"文件变更：{_compact(params, 240)}",
                    data={"diff": params, "display_detail": True},
                )
            )
        elif method == "turn/plan/updated":
            self._emit(
                Event(
                    kind="interaction",
                    source=self.source,
                    text=f"计划更新：{_compact(params, 240)}",
                    data={"plan": params, "display_detail": True},
                )
            )
        elif method in ("hook/started", "hook/completed", "model/safetyBuffering/updated", "model/verification", "contextCompaction"):
            self._emit(
                Event(
                    kind="system",
                    source=self.source,
                    text=f"{method}: {_compact(params, 200)}",
                    data=params,
                )
            )
        elif method == "serverRequest/resolved":
            pass  # 审批已解决，确认信号
        elif method == "error":
            err = params.get("error", params)
            message = str(err.get("message", "") or _compact(err, 200))
            # codex 断线自动重连是临时状态（"Reconnecting... n/5"），非任务失败；
            # 降级为 system（minimal 模式下不刷屏），避免被误读为致命错误。
            kind = "system" if message.startswith("Reconnecting") else "error"
            self._emit(Event(kind=kind, source=self.source, text=message, data=params))
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
        elif item_type == "plan":
            self._buffers.setdefault(item_id, {"kind": "plan", "parts": []})
            self._emit(
                Event(
                    kind="interaction",
                    source=self.source,
                    text=f"计划开始：{_compact(item, 220)}",
                    data={"plan": item, "item_id": item_id, "display_detail": True},
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
            if buf["kind"] == "plan":
                self._emit(
                    Event(
                        kind="interaction",
                        source=self.source,
                        text=f"计划：{text}",
                        data={"item_id": item_id, "plan": text, "display_detail": True},
                    )
                )
            else:
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
            self.after_tool(
                name,
                item if isinstance(item, dict) else {},
                {"is_error": is_error, "status": status, "output": output},
            )
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
            self._turn_output = str(text or "")
            if text:
                self._emit(Event(kind="text", source=self.source, text=str(text), data={"item_id": item_id}))
        elif item_type == "agentMessage" and buf and buf["parts"]:
            self._turn_output = "".join(buf["parts"])
        elif item_type == "plan" and not (buf and buf["parts"]):
            text = item.get("text", "") or item.get("plan", "")
            if text:
                self._emit(
                    Event(
                        kind="interaction",
                        source=self.source,
                        text=f"计划：{_compact(text, 500)}",
                        data={"item_id": item_id, "plan": text, "display_detail": True},
                    )
                )
        elif item_type == "reasoning" and not (buf and buf["parts"]):
            text = item.get("text", "")
            if text:
                self._emit(Event(kind="thinking", source=self.source, text=str(text), data={"item_id": item_id}))

    def _handle_turn_completed(self, params: dict) -> None:
        turn = params.get("turn", params)
        status = turn.get("status", "completed")
        self._turn_status = status
        self._turn_active = False
        self._result_ts = time.time()

        # flush 剩余缓冲
        for item_id, buf in list(self._buffers.items()):
            if buf["parts"]:
                text = "".join(buf["parts"])
                if buf["kind"] == "plan":
                    self._emit(
                        Event(
                            kind="interaction",
                            source=self.source,
                            text=f"计划：{text}",
                            data={"item_id": item_id, "plan": text, "display_detail": True},
                        )
                    )
                else:
                    kind = "text" if buf["kind"] == "text" else "thinking"
                    self._emit(Event(kind=kind, source=self.source, text=text, data={"item_id": item_id}))
        self._buffers.clear()

        output = _turn_output(turn, self._turn_output)
        self.run.output = output

        if status != "completed":
            error_info = turn.get("error", {})
            self.run.exit_code = 1
            self.run.status = "failed"
            detail = str(error_info.get("message", "") or _compact(error_info, 500))
            self.run.output = detail or f"turn 结束状态: {status}"
            self._emit(
                Event(
                    kind="error",
                    source=self.source,
                    text=f"turn 未成功完成（{status}）: {self.run.output}",
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

        decision = parse_loop_decision(output) if self._internal_loop and status == "completed" else None
        if self._internal_loop is not None and decision is None:
            self._finish_failure("任务内部 loop 未返回有效的 passed/needs_iteration JSON")
            return
        if (
            decision
            and decision.get("status") == "needs_iteration"
            and self._internal_loop is not None
            and self._loop_iteration + 1 < self._internal_loop.max_iterations
        ):
            self._loop_iteration += 1
            feedback = str(decision.get("feedback") or "请重新检查任务结果，修正未满足的验收条件。")
            self.run.exit_code = None
            self.run.status = "running"
            self._result_ts = None
            self._emit(
                Event(
                    kind="interaction",
                    source=self.source,
                    text=f"任务内部 loop：第 {self._loop_iteration} 轮未满足，继续当前 thread",
                    data={"internal_loop": True, "iteration": self._loop_iteration, "feedback": feedback},
                )
            )
            self._start_followup_turn(self._loop_prompt(feedback), internal=True)
            return

        if self._internal_loop is not None and decision.get("status") == "needs_iteration":
            self._finish_failure(
                f"任务内部 loop 已达到最大轮次（{self._internal_loop.max_iterations}），仍未满足退出条件"
            )
            return

        # completed
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
        self.run.output = output or (output_parts[0] if output_parts else "")

        self._emit(
            Event(
                kind="result",
                source=self.source,
                text=self.run.output,
                data={
                    "status": status,
                    "cost_usd": self.run.cost_usd,
                    "turn_id": self._turn_id,
                    "internal_loop": bool(self._internal_loop),
                    "loop_iterations": self._loop_iteration + 1,
                    "loop_decision": decision,
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

        mode = self.cfg.approval.mode
        req = Event(kind="permission_request", source=self.source, text=text, data={**data, "auto": mode == "auto"})
        self._emit(req)

        policy_tool = {
            "item/commandExecution/requestApproval": "run_command",
            "item/fileChange/requestApproval": "edit_file",
        }.get(method)
        hook = self.before_tool(policy_tool, params) if policy_tool else None
        if hook is not None and not hook.allowed:
            self._decide_approval(msg, False, f"工具钩子拒绝：{hook.message}")
            return
        policy = self.tool_decision(policy_tool) if policy_tool else None
        if policy is not None and not policy.allowed:
            self._decide_approval(msg, False, f"工具策略拒绝：{policy.reason}")
            return

        if mode == "auto":
            self._decide_approval(msg, bool(self.cfg.approval.default_allow), "auto 模式", auto=True)
        elif mode == "log":
            self._decide_approval(msg, False, "log 模式：仅记录，默认拒绝")
        else:  # ask_console —— 阻塞 pump 等待 :allow/:deny
            got, allowed, _feedback = self.broker.wait_decision(rid, kind="permission", run=self.run, event=req)
            if got and allowed is not None:
                self._decide_approval(msg, allowed)
            else:
                self._decide_approval(msg, False, "审批超时，默认拒绝")

    def _decide_approval(self, msg: dict, allowed: bool, note: str = "", auto: bool = False) -> None:
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
                    "content": {} if allowed else None,
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
                data={"allowed": allowed, "id": str(rid), "auto": auto},
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

    def _send_steer(self, text: str) -> bool:
        """仅在 pump 线程内调用，确保 turn 状态与发送操作串行化。"""
        if not self._thread_id or not self._turn_id:
            return False
        rid = self._next_id()
        return self._write(
            {
                "id": rid,
                "method": "turn/steer",
                "params": {
                    "threadId": self._thread_id,
                    "input": [{"type": "text", "text": text}],
                    "expectedTurnId": self._turn_id,
                },
            }
        )

    def _initial_prompt(self) -> str:
        if self._internal_loop is None:
            return self.prompt
        loop = self._internal_loop
        return (
            f"{self.prompt}\n\n"
            "这是一个由 Codex App Server 驱动的任务内部迭代。请实际检查工作区和验收条件；"
            "如果已经满足，返回 status=passed；如果不满足，先尽可能修正，并返回 status=needs_iteration "
            "以及具体 feedback。不要要求编排器重跑整张任务图。"
            f"最多允许 {loop.max_iterations} 个内部 turn。"
        )

    def _loop_prompt(self, feedback: str) -> str:
        extra = self._internal_loop.feedback_prompt if self._internal_loop else ""
        condition = self._internal_loop.exit_condition if self._internal_loop else ""
        return (
            "继续当前任务的内部迭代。请根据上一轮反馈检查实际文件、运行必要的验证并修正问题。\n"
            f"退出条件：{condition or '满足任务验收标准'}\n"
            f"上一轮反馈：{feedback}\n"
            f"额外迭代要求：{extra or '无'}\n"
            "完成后仍然只按约定的结构化结果报告：status=passed 或 status=needs_iteration，并给出 feedback。"
        )

    def _reset_turn_output(self) -> None:
        self._turn_output = ""
        self._buffers.clear()

    def _start_followup_turn(self, text: str, *, internal: bool) -> bool:
        """在同一个 thread 上启动新 turn；turn/steer 只用于 active turn。"""
        if not self._thread_id or not self.channel or not self.channel.is_alive():
            return False
        self._reset_turn_output()
        self._turn_active = False
        self._turn_status = "inProgress"
        self.run.exit_code = None
        self.run.status = "running"
        self._result_ts = None
        rid = self._next_id()
        self._pending_turn_starts[rid] = internal
        c = self.cfg.codex
        ok = self._write(
            {
                "id": rid,
                "method": "turn/start",
                "params": {
                    "threadId": self._thread_id,
                    "input": [{"type": "text", "text": text}],
                    "cwd": self.workdir,
                    "approvalPolicy": _approval_policy(c.approval_policy),
                    "sandboxPolicy": _sandbox_policy(c.sandbox, self.workdir),
                    **({"model": c.model} if c.model else {}),
                    **({"outputSchema": _LOOP_OUTPUT_SCHEMA} if self._internal_loop else {}),
                },
            }
        )
        if not ok:
            self._pending_turn_starts.pop(rid, None)
            self._finish_failure("无法发送 follow-up turn/start")
        return ok


def _usage_text(usage) -> str:
    """将 App Server 的 token/cost 统计压成一行详情。"""
    if not isinstance(usage, dict):
        return _compact(usage, 180)
    labels = (
        ("input_tokens", "in"),
        ("output_tokens", "out"),
        ("total_tokens", "total"),
        ("total_cost_usd", "cost"),
    )
    parts = [f"{label}={usage[key]}" for key, label in labels if usage.get(key) is not None]
    return " ".join(parts) or _compact(usage, 180)


def _approval_policy(value: str) -> str:
    """转成本机 codex-cli 0.147 app-server 实际接受的 wire 值。

    官方文档已经列出 camelCase 的 onRequest/unlessTrusted，但 0.147.0
    的协议实现仍接受 on-request/untrusted；保留两套配置输入的兼容性。
    """
    return {
        "onRequest": "on-request",
        "on-request": "on-request",
        "onFailure": "on-failure",
        "on-failure": "on-failure",
        "unlessTrusted": "untrusted",
        "unless-trusted": "untrusted",
        "untrusted": "untrusted",
        "granular": "granular",
        "never": "never",
    }.get(str(value or "").strip(), "on-request")


def _sandbox_mode(value: str) -> str:
    """thread/start 的 sandbox 使用协议中的 camelCase 模式名。"""
    return {
        "read-only": "readOnly",
        "workspace-write": "workspaceWrite",
        "danger-full-access": "dangerFullAccess",
    }.get(str(value or "").strip(), str(value or "workspaceWrite"))


def _sandbox_policy(value: str, workdir: str) -> dict:
    """turn/start 的 sandboxPolicy 使用当前协议对象，而不是旧字符串。"""
    mode = _sandbox_mode(value)
    if mode == "workspaceWrite":
        return {"type": mode, "writableRoots": [workdir]}
    return {"type": mode}


def _turn_output(turn: dict, fallback: str) -> str:
    """从 completed turn 中提取最终 agentMessage；item 事件是主要来源。"""
    for key in ("structuredOutput", "structured_output", "output"):
        value = turn.get(key)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, str) and value.strip():
            return value
    for item in reversed(turn.get("items") or []):
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            value = item.get("text", "")
            if isinstance(value, str) and value.strip():
                return value
    return fallback or ""
