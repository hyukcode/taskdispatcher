
from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent import futures

from .approvals import ApprovalBroker
from .config import Config
from .models import Event, TaskLoop, TaskRun

try:
    import claude_agent_sdk as sdk
except ImportError: 
    sdk = None


class SdkClaudeRunner:
    source = "claude"
    self_handles_approval = True

    def __init__(self, cfg: Config, run: TaskRun, workdir: str, on_event, prompt: str, broker=None):
        self.cfg = cfg
        self.run = run
        self.workdir = workdir
        self.on_event = on_event
        self.prompt = prompt
        self.broker = broker or ApprovalBroker(cfg.approval)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tool_names: dict[str, str] = {}
        self._interactions: list[str] = []
        self._result_ts: float | None = None
        self._injection_ts: float | None = None
        self._last_event_ts: float = time.time()
        self._settle = float(getattr(cfg.claude, "completion_idle", 5.0))
        self._internal_loop: TaskLoop | None = None
        self._loop_iteration = 0
        self._next_loop_prompt = ""

    def set_internal_loop(self, loop: TaskLoop | None) -> None:
        self._internal_loop = loop if loop and loop.enabled else None
        if self._internal_loop is not None:
            self._internal_loop.max_iterations = max(1, int(self._internal_loop.max_iterations))

    def start(self) -> None:
        if sdk is None:
            raise RuntimeError("未安装 claude-agent-sdk：请先 `pip install claude-agent-sdk`（仅 --use-sdk 需要）")
        self.run.started_at = time.time()
        self._thread = threading.Thread(target=self._amain, daemon=True, name=f"sdk-claude-{self.run.task.id}")
        self._thread.start()

    def _amain(self) -> None:
        try:
            asyncio.run(self._arun())
        except Exception as e:
            self._emit(Event(kind="error", source=self.source, text=f"SDK 事件泵异常: {e}"))
        finally:
            self.run.ended_at = time.time()

    async def _arun(self) -> None:
        self._loop = asyncio.get_running_loop()
        opts = self._build_options()
        async with sdk.ClaudeSDKClient(options=opts) as client:
            self._client = client
            await client.query(self._sys_prompt())
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
                await client.query(self._next_loop_prompt)

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

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def is_done(self) -> bool:
        if not self.is_alive():
            return True
        if self._result_ts is None:
            return False
        ref = max(self._result_ts, self._injection_ts or 0)
        now = time.time()
        return (now - ref) > self._settle and (now - self._last_event_ts) > self._settle

    def finalize(self) -> None:
        """收尾：断开 SDK 传输，让 receive_response() 结束，事件泵线程退出。"""
        self._stop.set()
        client = self._client
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass

    def send_message(self, text: str) -> bool:
        if not self._client or not self._loop or self._stop.is_set():
            return False
        try:
            asyncio.run_coroutine_threadsafe(self._client.query(text), self._loop)
            self._interactions.append(text)
            self._injection_ts = time.time()
            self._emit(Event(kind="user_message", source=self.source, text=text))
            return True
        except Exception:  # noqa: BLE001
            return False

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

    @property
    def pending_approval_ids(self) -> list[str]:
        return self.broker.pending_ids

    def approval_respond(self, req_id: str, allowed: bool) -> bool:
        return self.broker.resolve(req_id, allowed=allowed)

    def _emit(self, event: Event) -> None:
        self._last_event_ts = time.time()
        self.run.events.append(event)
        self.on_event(self.run, event)

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
            self.run.exit_code = 0
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
            content = block.content
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
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

        decision = _loop_decision(self.run.output) if self._internal_loop and not msg.is_error else None
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

        self.run.exit_code = 0 if not msg.is_error else 1
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


def _compact(obj, width: int) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    return s if len(s) <= width else s[: width - 1] + "…"


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
                continue
            if isinstance(data, dict):
                return data
    try:
        data = vars(value)
    except TypeError:
        return {}
    return dict(data) if isinstance(data, dict) else {}


def _loop_decision(output: str) -> dict | None:
    text = (output or "").strip()
    if not text:
        return None
    candidates = [text]
    if "```" in text:
        candidates.extend(part.strip() for part in text.split("```") if part.strip())
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            start = candidate.find("{")
            if start < 0:
                continue
            try:
                value, _ = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict) and value.get("status") in {"passed", "needs_iteration"}:
            return value
    return None
