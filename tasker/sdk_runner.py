
from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent import futures

from .config import Config
from .formatting import compact_json as _compact
from .loop_protocol import parse_loop_decision
from .models import Event, TaskRun
from .policy_hooks import HookChain
from .runner_base import EventSink, RunnerBase
from .tool_catalog import ToolCatalog

try:
    import claude_agent_sdk as sdk
except ImportError: 
    sdk = None


logger = logging.getLogger(__name__)


class SdkClaudeRunner(RunnerBase):
    source = "claude"
    config_key = "claude"

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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._tool_names: dict[str, str] = {}
        self._tool_inputs: dict[str, dict] = {}
        self._next_loop_prompt = ""

    def _prepare_start(self) -> None:
        if sdk is None:
            raise RuntimeError("未安装 claude-agent-sdk：请先 `pip install claude-agent-sdk`（仅 --use-sdk 需要）")

    def _run_transport(self) -> None:
        asyncio.run(self._arun())

    async def _arun(self) -> None:
        self._loop = asyncio.get_running_loop()
        opts = self._build_options()
        async with sdk.ClaudeSDKClient(options=opts) as client:
            self._client = client
            self._query_lock = asyncio.Lock()
            await self._query(client, self._sys_prompt())
            input_task = asyncio.create_task(self._consume_input(client))
            try:
                while not self._stop.is_set():
                    continue_loop = False
                    async for msg in client.receive_response():
                        if self._stop.is_set():
                            break
                        try:
                            continue_loop = self._handle(msg) or continue_loop
                        except Exception as e:
                            self._emit(Event(kind="error", source=self.source, text=f"事件处理失败: {e}"))
                    if not continue_loop:
                        break
                    await self._query(client, self._next_loop_prompt)
            finally:
                input_task.cancel()
                try:
                    await input_task
                except asyncio.CancelledError:
                    pass

    async def _query(self, client, text: str) -> None:
        async with self._query_lock:
            await client.query(text)

    async def _consume_input(self, client) -> None:
        """在 SDK 所属事件循环中顺序消费用户注入，避免并发 query。"""
        while not self._stop.is_set():
            available, text = self._take_input_nowait()
            if not available:
                await asyncio.sleep(0.05)
                continue
            if text is None:
                return
            try:
                await self._query(client, text)
            except Exception as exc:  # noqa: BLE001
                self._emit(Event(kind="error", source=self.source, text=f"注入消息发送失败: {exc}"))

    def _build_options(self):
        c = self.cfg.claude
        mode = c.permission_mode or "default"
        if mode == "acceptEdits":
            mode = "default"
            self._emit(
                Event(
                    kind="system",
                    source=self.source,
                    text="permission_mode 已从 acceptEdits 切换为 default（can_use_tool 需 ask 决策才触发）；审批由 approval 策略处理",
                )
            )
        kwargs = {
            "permission_mode": mode,
            "can_use_tool": self._can_use_tool,
            "cwd": self.workdir,
            "extra_args": {"verbose": None},
        }
        if c.model:
            kwargs["model"] = c.model
        if c.allowed_tools:
            kwargs["allowed_tools"] = c.allowed_tools
        if c.disallowed_tools:
            kwargs["disallowed_tools"] = c.disallowed_tools
        return sdk.ClaudeAgentOptions(**kwargs)

    def _sys_prompt(self) -> str:
        prompt = (
            "你是 tasker 多智能体编排中的一个子任务 worker。当前工作目录即任务目录。\n"
            "协作提示：编排器可能在运行中向你注入后续要求（用户中途修改/追问），收到后请按新要求调整再继续。\n\n"
            + self.prompt
        )
        if self._internal_loop is not None:
            prompt += (
                "\n\n这是一个任务内部迭代。请检查实际工作区和完成标准：满足时输出 JSON "
                '{"status":"passed","feedback":"..."}；不满足时先修正问题，再输出 '
                '{"status":"needs_iteration","feedback":"具体还需要做什么"}。'
                f"最多执行 {self._internal_loop.max_iterations} 轮，不要要求编排器重跑整张任务图。"
            )
        return prompt

    def _finalize_transport(self) -> None:
        """断开 SDK 传输，让 receive_response() 结束。"""
        client = self._client
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                logger.debug("Claude SDK disconnect 失败", exc_info=True)

    def _stop_transport(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                logger.debug("Claude SDK stop disconnect 失败", exc_info=True)

    async def _can_use_tool(self, tool_name: str, input_data: dict, context) -> sdk.PermissionResult:
        tool_use_id = (context.tool_use_id if context is not None else None) or str(time.time())
        mode = self.cfg.approval.mode
        req = Event(
            kind="permission_request",
            source=self.source,
            text=f"{tool_name} {_compact(input_data, 120)}",
            data={"tool": tool_name, "input": input_data, "id": tool_use_id, "tool_use_id": tool_use_id, "auto": mode == "auto"},
        )
        self._emit(req)

        hook = self.before_tool(tool_name, input_data if isinstance(input_data, dict) else {})
        if not hook.allowed:
            self._permission_result(req, False, hook.message)
            return sdk.PermissionResultDeny(message=hook.message)

        policy = self.tool_decision(tool_name)
        if policy is not None and not policy.allowed:
            self._permission_result(req, False, policy.reason)
            return sdk.PermissionResultDeny(message=policy.reason)

        if mode == "auto":
            allowed = self.cfg.approval.default_allow
            self._permission_result(req, allowed)
            return sdk.PermissionResultAllow() if allowed else sdk.PermissionResultDeny(message="auto 模式拒绝")
        if mode == "log":
            self._permission_result(req, False, "log 模式：仅记录，默认拒绝")
            return sdk.PermissionResultDeny(message="log 模式拒绝")

        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def _resolve(allowed: bool, feedback: str) -> None:
            if not fut.done():
                loop.call_soon_threadsafe(fut.set_result, allowed)

        self.broker.register_async(tool_use_id, kind="permission", run=self.run, event=req, resolver=_resolve)
        try:
            allowed = await asyncio.wait_for(fut, self.cfg.approval.timeout)
        except (asyncio.TimeoutError, futures.TimeoutError):
            allowed = False
        finally:
            self.broker.unregister(tool_use_id)
        self._permission_result(req, allowed)
        return sdk.PermissionResultAllow() if allowed else sdk.PermissionResultDeny(message="用户拒绝")

    def _permission_result(self, req: Event, allowed: bool, note: str = "") -> None:
        head = "批准" if allowed else "拒绝"
        self._emit(
            Event(
                kind="permission_result",
                source=self.source,
                text=f"{head} {note or req.text}",
                data={"allowed": allowed, "id": req.data.get("id"), "auto": bool(req.data.get("auto"))},
            )
        )

    def _handle(self, msg) -> bool:
        if isinstance(msg, sdk.SystemMessage):
            self._handle_system(msg)
        elif isinstance(msg, sdk.AssistantMessage):
            for block in msg.content or []:
                self._handle_block(block)
        elif isinstance(msg, sdk.UserMessage):
            content = msg.content
            if isinstance(content, str):
                if content.strip():
                    self._emit(Event(kind="text", source=self.source, text=content))
            else:
                for block in content or []:
                    self._handle_block(block)
        elif isinstance(msg, sdk.ResultMessage):
            return self._handle_result(msg)
        elif isinstance(msg, sdk.StreamEvent):
            pass 
        else:
            self._emit(Event(kind="raw", source=self.source, text=str(msg)[:2000]))
        return False

    def _handle_system(self, msg) -> None:
        sub = msg.subtype
        data = msg.data or {}
        if sub == "init":
            self._emit(
                Event(
                    kind="system",
                    source=self.source,
                    text=f"会话初始化（SDK） cwd={data.get('cwd', '')}",
                    data={"session_id": data.get("session_id"), "display": True},
                )
            )
        elif sub == "thinking_tokens":
            pass
        elif sub in ("task_started", "task_progress", "task_notification"):
            self._emit(
                Event(
                    kind="interaction",
                    source=self.source,
                    text=f"{sub}: {data.get('description', '')}",
                    data={"task_id": data.get("task_id"), "tool_use_id": data.get("tool_use_id"), "subtype": sub},
                )
            )
        elif sub == "permission_denied":
            self._emit(Event(kind="permission_result", source=self.source, text="权限被拒", data={"allowed": False, "detail": data}))
        else:
            self._emit(Event(kind="system", source=self.source, text=f"{sub} {_compact(data, 160)}", data=data))

    def _handle_block(self, block) -> None:
        if isinstance(block, sdk.ThinkingBlock):
            self._emit(Event(kind="thinking", source=self.source, text=str(block.thinking or ""), data={"signature": block.signature}))
        elif isinstance(block, sdk.TextBlock):
            self._emit(Event(kind="text", source=self.source, text=str(block.text or "")))
        elif isinstance(block, (sdk.ToolUseBlock, sdk.ServerToolUseBlock)):
            self._tool_names[block.id] = block.name
            self._tool_inputs[block.id] = block.input or {}
            self._emit(
                Event(
                    kind="tool_use",
                    source=self.source,
                    text=block.name,
                    data={"tool": block.name, "input": block.input or {}, "id": block.id},
                )
            )
        elif isinstance(block, (sdk.ToolResultBlock, sdk.ServerToolResultBlock)):
            name = self._tool_names.get(str(block.tool_use_id), "?")
            input_data = self._tool_inputs.get(str(block.tool_use_id), {})
            content = block.content
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            self.after_tool(
                name,
                input_data,
                {"is_error": bool(getattr(block, "is_error", False)), "content": content},
            )
            self._emit(
                Event(
                    kind="tool_result",
                    source=self.source,
                    text=content,
                    data={
                        "tool": name,
                        "is_error": bool(getattr(block, "is_error", False)),
                        "tool_use_id": block.tool_use_id,
                    },
                )
            )

    def _handle_result(self, msg) -> bool:
        self._result_ts = time.time()
        self.run.output = str(msg.result or "")
        self.run.cost_usd = float(msg.total_cost_usd or 0.0)
        usage = _usage_dict(getattr(msg, "usage", None))
        if usage:
            usage.setdefault("cost_usd", self.run.cost_usd)
            self._emit(
                Event(
                    kind="usage",
                    source=self.source,
                    text=_compact(usage, 180),
                    data=usage,
                )
            )

        decision = parse_loop_decision(self.run.output) if self._internal_loop and not msg.is_error else None
        if self._internal_loop is not None and not msg.is_error and decision is None:
            self.run.exit_code = 1
            self.run.status = "failed"
            self.run.error = "任务内部 loop 未返回有效的 passed/needs_iteration JSON"
            self._emit(Event(kind="error", source=self.source, text=self.run.error))
            return False
        if (
            decision
            and decision.get("status") == "needs_iteration"
            and self._internal_loop is not None
            and self._loop_iteration + 1 < self._internal_loop.max_iterations
        ):
            self._loop_iteration += 1
            feedback = str(decision.get("feedback") or "请重新检查任务结果并修正未满足的条件。")
            self.run.status = "running"
            self.run.exit_code = None
            self._result_ts = None
            self._next_loop_prompt = (
                "继续当前任务的内部迭代。请在同一个工作区中根据上一轮反馈检查、修正并验证。\n"
                f"退出条件：{self._internal_loop.exit_condition or '满足任务验收标准'}\n"
                f"上一轮反馈：{feedback}\n"
                "完成后再次输出 status=passed 或 status=needs_iteration，并给出 feedback。"
            )
            self._emit(
                Event(
                    kind="interaction",
                    source=self.source,
                    text=f"任务内部 loop：第 {self._loop_iteration} 轮未满足，继续同一 Claude SDK 会话",
                    data={"internal_loop": True, "iteration": self._loop_iteration, "feedback": feedback},
                )
            )
            return True

        if (
            self._internal_loop is not None
            and not msg.is_error
            and decision.get("status") == "needs_iteration"
        ):
            self.run.exit_code = 1
            self.run.status = "failed"
            self.run.error = f"任务内部 loop 已达到最大轮次（{self._internal_loop.max_iterations}），仍未满足退出条件"
            self._emit(Event(kind="error", source=self.source, text=self.run.error))
            return False

        self.run.exit_code = 0 if not msg.is_error else 1
        self.run.status = "success" if not msg.is_error else "failed"
        if msg.is_error:
            self.run.error = self.run.output or "Claude SDK 返回错误"
        for d in msg.permission_denials or []:
            text = d.get("message", "") if isinstance(d, dict) else str(d)
            self._emit(Event(kind="permission_result", source=self.source, text=str(text), data={"allowed": False, "detail": d}))
        self._emit(
            Event(
                kind="result",
                source=self.source,
                text=self.run.output,
                data={
                    "stop_reason": msg.stop_reason,
                    "terminal_reason": msg.terminal_reason,
                    "cost_usd": self.run.cost_usd,
                    "usage": usage,
                    "permission_denials": len(msg.permission_denials or []),
                },
            )
        )
        return False


def _usage_dict(value) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    for method in ("model_dump", "dict"):
        fn = getattr(value, method, None)
        if callable(fn):
            try:
                data = fn()
            except Exception:
                logger.debug("读取 SDK usage 对象失败", exc_info=True)
                continue
            if isinstance(data, dict):
                return data
    try:
        data = vars(value)
    except TypeError:
        return {}
    return dict(data) if isinstance(data, dict) else {}
