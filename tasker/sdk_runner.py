"""基于官方 claude-agent-sdk 的 Claude 采集器（可选后端，--use-sdk 启用）。

与 ClaudeRunner（手写 stream-json 解析）相比：
- 事件更结构化：AssistantMessage / SystemMessage / ResultMessage 等类型化消息，
  block 是 ThinkingBlock / TextBlock / ToolUseBlock / ToolResultBlock。
- 关键升级：can_use_tool 回调让 headless 下也能收到原本"交互 TTY 才出现"的权限请求，
  并由编排器程序化批准/拒绝（现方案只能 acceptEdits 放行或 attach 看弹窗）。
- 中途注入：SDK 无官方"向进行中 turn push 任意 prompt"的 API，
  这里仍用 ClaudeSDKClient.query() 在持续会话上发新用户消息（底层与 stream-json 同一传输）。

依赖：`pip install claude-agent-sdk`（可选，仅 --use-sdk 时需要）。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent import futures

from .approvals import ApprovalBroker
from .config import Config
from .models import Event, TaskRun

try:
    import claude_agent_sdk as sdk
except ImportError:  # 未安装 SDK 时使用 sdk_runner 会给出清晰报错
    sdk = None


class SdkClaudeRunner:
    """与 ClaudeRunner 相同的对外接口（start/is_done/finalize/stop/send_message/is_alive），
    额外暴露 approval_respond/pending_approval_ids 供调度器把 :allow/:deny 送达 can_use_tool。
    """

    source = "claude"
    # 审批请求由本 runner 的 can_use_tool 自行处理，调度器的 ApprovalPolicy 不要再插手
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

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if sdk is None:
            raise RuntimeError("未安装 claude-agent-sdk：请先 `pip install claude-agent-sdk`（仅 --use-sdk 需要）")
        self.run.started_at = time.time()
        self._thread = threading.Thread(target=self._amain, daemon=True, name=f"sdk-claude-{self.run.task.id}")
        self._thread.start()

    def _amain(self) -> None:
        try:
            asyncio.run(self._arun())
        except Exception as e:  # noqa: BLE001 —— 事件泵线程永不因单点异常退出
            self._emit(Event(kind="error", source=self.source, text=f"SDK 事件泵异常: {e}"))
        finally:
            self.run.ended_at = time.time()

    async def _arun(self) -> None:
        self._loop = asyncio.get_running_loop()
        opts = self._build_options()
        async with sdk.ClaudeSDKClient(options=opts) as client:
            self._client = client
            await client.query(self._sys_prompt())
            async for msg in client.receive_response():
                if self._stop.is_set():
                    break
                try:
                    self._handle(msg)
                except Exception as e:  # noqa: BLE001
                    self._emit(Event(kind="error", source=self.source, text=f"事件处理失败: {e}"))

    def _build_options(self):
        c = self.cfg.claude
        # can_use_tool 只在 permission 决策为 "ask" 时触发；acceptEdits 下 Bash 等是 auto-deny 不会回调。
        # 因此 SDK 后端把默认的 acceptEdits 翻译成 default：需要权限的工具都走 can_use_tool 由审批策略决定。
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
            # SDK 的 extra_args 是 dict：key 不含前导 --（SDK 会自己加），value None 表示纯开关
            "extra_args": {"verbose": None},
        }
        # 只在非空时传：SDK 对显式 None 的 allowed_tools 有 bug（_warn_if_can_use_tool_shadowed 会 dict.fromkeys(None)）
        if c.model:
            kwargs["model"] = c.model
        if c.allowed_tools:
            kwargs["allowed_tools"] = c.allowed_tools
        if c.disallowed_tools:
            kwargs["disallowed_tools"] = c.disallowed_tools
        return sdk.ClaudeAgentOptions(**kwargs)

    def _sys_prompt(self) -> str:
        return (
            "你是 tasker 多智能体编排中的一个子任务 worker。当前工作目录即任务目录。\n"
            "协作提示：编排器可能在运行中向你注入后续要求（用户中途修改/追问），收到后请按新要求调整再继续。\n\n"
            + self.prompt
        )

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

    # ---------- 中途注入 ----------
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

    # ---------- can_use_tool：headless 程序化审批 ----------
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

        # ask_console：在 CLI 展示审批请求，阻塞等待用户 :allow/:deny
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
        """调度器 :allow/:deny 送达：解析阻塞在 can_use_tool 里的等待。"""
        return self.broker.resolve(req_id, allowed=allowed)

    # ---------- 事件泵 ----------
    def _emit(self, event: Event) -> None:
        self._last_event_ts = time.time()
        self.run.events.append(event)
        self.on_event(self.run, event)

    def _handle(self, msg) -> None:
        # SDK 消息没有 .type 字段，用 isinstance 分派
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
            self._handle_result(msg)
        elif isinstance(msg, sdk.StreamEvent):
            pass  # 流式增量事件，prototype 不展开
        else:
            self._emit(Event(kind="raw", source=self.source, text=str(msg)[:2000]))

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
                    data={"session_id": data.get("session_id")},
                )
            )
        elif sub == "thinking_tokens":
            pass  # token 计数噪音，忽略
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
            # 未走 can_use_tool 的自动拒绝（如 acceptEdits 直拒），作为审批结果呈现
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

    def _handle_result(self, msg) -> None:
        self._result_ts = time.time()
        self.run.output = str(msg.result or "")
        self.run.exit_code = 0 if not msg.is_error else 1
        self.run.cost_usd = float(msg.total_cost_usd or 0.0)
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
                    "permission_denials": len(msg.permission_denials or []),
                },
            )
        )


def _compact(obj, width: int) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(obj)
    return s if len(s) <= width else s[: width - 1] + "…"
