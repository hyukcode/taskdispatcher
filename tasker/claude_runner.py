"""Claude Code 采集器。

headless 方式：claude -p --output-format stream-json --verbose --input-format stream-json
- 从 stdout 逐行解析事件：thinking（思维链）、tool_use、tool_result、task 事件、result。
- stdin 保持打开：send_message() 以 stream-json 用户消息实时注入，实现"中途修改/继续要求"。
- 权限相关：headless 下权限弹窗不触发（交互 TTY 才有），工具执行/拒绝会体现为
  tool_result 与 result.permission_denials，一并采集。
"""
from __future__ import annotations

import json
import os
import threading
import time

from .config import Config
from .models import Event, TaskRun
from .spawn import ProcChannel, resolve_binary, start_process


class ClaudeRunner:
    source = "claude"

    def __init__(self, cfg: Config, run: TaskRun, workdir: str, on_event, prompt: str, broker=None):
        self.cfg = cfg
        self.run = run
        self.workdir = workdir
        self.on_event = on_event
        self.prompt = prompt
        self.broker = broker  # 非自管理审批（headless stream-json 无 permission_request），保留统一构造签名
        self.channel: ProcChannel | None = None
        self._tool_names: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interactions: list[str] = []
        # claude 发出 result 后不会自己退出（stdin 仍开），需要 settle 后关闭 stdin
        self._result_ts: float | None = None
        self._injection_ts: float | None = None
        self._last_event_ts: float = time.time()
        self._settle = float(getattr(cfg.claude, "completion_idle", 5.0))

    # ---------- 完成判定 ----------
    def is_done(self) -> bool:
        if not self.is_alive():
            return True
        if self._result_ts is None:
            return False
        ref = max(self._result_ts, self._injection_ts or 0)
        now = time.time()
        # 结果之后：既无更新的注入，也无任何 claude 活动，持续 settle 秒 → 认为子任务完成
        return (now - ref) > self._settle and (now - self._last_event_ts) > self._settle

    def finalize(self) -> None:
        """收到最终 result 后：关闭 stdin 让 claude 自然退出，等待一小段，必要时终止。"""
        if not self.channel or not self.channel.is_alive():
            return
        self.channel.close_stdin()
        try:
            self.channel.proc.wait(timeout=8)
        except Exception:
            self.channel.stop()

    # ---------- 生命周期 ----------
    def build_args(self) -> list[str]:
        c = self.cfg.claude
        args = [resolve_binary(c.binary), "-p", "--output-format", "stream-json", "--verbose", "--input-format", "stream-json"]
        if c.model:
            args += ["--model", c.model]
        if c.permission_mode and c.permission_mode != "default":
            args += ["--permission-mode", c.permission_mode]
        if c.allowed_tools:
            args += ["--allowedTools", ",".join(c.allowed_tools)]
        if c.disallowed_tools:
            args += ["--disallowedTools", ",".join(c.disallowed_tools)]
        args += c.extra_args
        return args

    def _sys_note(self) -> str:
        return (
            "你是 tasker 多智能体编排中的一个子任务 worker。当前工作目录即任务目录。\n"
            "协作提示：编排器可能在运行中向你注入后续要求（用户中途修改/追问），收到后请按新要求调整再继续。"
        )

    def start(self) -> ProcChannel:
        self.run.started_at = time.time()
        args = self.build_args()
        extra = ["--append-system-prompt", self._sys_note()]
        self.channel = start_process(args + extra, workdir=self.workdir, name=f"claude-{self.run.task.id}")
        # 初始用户消息（stream-json）
        first = self._user_msg_json(self.prompt)
        self.channel.write(first + "\n")
        self._thread = threading.Thread(target=self._pump, daemon=True, name=f"claude-{self.run.task.id}")
        self._thread.start()
        return self.channel

    # ---------- 实时注入 ----------
    def send_message(self, text: str) -> bool:
        if not self.channel or not self.channel.is_alive():
            return False
        ok = self.channel.write(self._user_msg_json(text) + "\n")
        if ok:
            self._interactions.append(text)
            self._injection_ts = time.time()
            self._emit(Event(kind="user_message", source=self.source, text=text))
        return ok

    def stop(self) -> None:
        self._stop.set()
        if self.channel:
            self.channel.stop()

    def is_alive(self) -> bool:
        return bool(self.channel and self.channel.is_alive())

    @staticmethod
    def _user_msg_json(text: str) -> str:
        return json.dumps(
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
            ensure_ascii=False,
        )

    # ---------- 事件泵 ----------
    def _emit(self, event: Event) -> None:
        self._last_event_ts = time.time()
        self.run.events.append(event)
        self.on_event(self.run, event)

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
                try:
                    self._handle(data)
                except Exception as e:  # noqa: BLE001 —— 单条事件解析失败不打断整个泵线程
                    self._emit(Event(kind="error", source=self.source, text=f"事件解析失败: {e}"))
        finally:
            self.run.ended_at = time.time()

    def _handle(self, data: dict) -> None:
        t = data.get("type")
        if t == "system":
            sub = data.get("subtype", "")
            if sub == "init":
                self.run.exit_code = 0
                self._emit(
                    Event(
                        kind="system",
                        source=self.source,
                        text=f"会话初始化 session_id={data.get('session_id','')[:8]}… cwd={data.get('cwd','')}",
                        data={"session_id": data.get("session_id")},
                    )
                )
            elif sub == "thinking_tokens":
                pass  # token 计数噪音，忽略
            elif sub in ("task_started", "task_progress", "task_notification", "interrupt_*"):
                self._emit(
                    Event(
                        kind="interaction",
                        source=self.source,
                        text=f"{sub}: {data.get('description','')}",
                        data={"task_id": data.get("task_id"), "tool_use_id": data.get("tool_use_id"), "subtype": sub},
                    )
                )
            else:
                self._emit(Event(kind="system", source=self.source, text=f"{sub} {data.get('description','')}", data=data))
        elif t == "assistant":
            msg = data.get("message", {})
            for block in msg.get("content", []):
                btype = block.get("type")
                if btype == "thinking":
                    self._emit(
                        Event(
                            kind="thinking",
                            source=self.source,
                            text=str(block.get("thinking", "")),
                            data={"signature": block.get("signature")},
                        )
                    )
                elif btype == "redacted_thinking":
                    self._emit(Event(kind="thinking", source=self.source, text="[redacted thinking]"))
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    tid = block.get("id")
                    if tid:
                        self._tool_names[tid] = name
                    self._emit(
                        Event(
                            kind="tool_use",
                            source=self.source,
                            text=name,
                            data={"tool": name, "input": block.get("input", {}), "id": tid},
                        )
                    )
                elif btype == "text":
                    self._emit(Event(kind="text", source=self.source, text=str(block.get("text", ""))))
        elif t == "user":
            # 可能是 tool_result 回灌，或子代理消息
            msg = data.get("message", {})
            content = msg.get("content", [])
            for block in content if isinstance(content, list) else [content]:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    name = self._tool_names.get(str(block.get("tool_use_id")), "?")
                    result_text = block.get("content", "")
                    if not isinstance(result_text, str):
                        result_text = json.dumps(result_text, ensure_ascii=False, default=str)
                    raw_extra = data.get("tool_use_result", {})
                    extra = raw_extra if isinstance(raw_extra, dict) else {}
                    self._emit(
                        Event(
                            kind="tool_result",
                            source=self.source,
                            text=result_text,
                            data={
                                "tool": name,
                                "is_error": bool(block.get("is_error", extra.get("is_error", False))),
                                "stdout": extra.get("stdout", "") if isinstance(extra.get("stdout"), str) else json.dumps(extra.get("stdout", ""), ensure_ascii=False, default=str),
                                "stderr": extra.get("stderr", "") if isinstance(extra.get("stderr"), str) else json.dumps(extra.get("stderr", ""), ensure_ascii=False, default=str),
                                "interrupted": bool(extra.get("interrupted", False)),
                                "tool_use_id": block.get("tool_use_id"),
                            },
                        )
                    )
                elif block.get("type") == "text":
                    self._emit(Event(kind="text", source=self.source, text=str(block.get("text", ""))))
        elif t == "result":
            self._result_ts = time.time()
            self.run.output = str(data.get("result", ""))
            self.run.exit_code = 0 if not data.get("is_error") else data.get("error")
            self.run.cost_usd = float(data.get("total_cost_usd") or 0.0)
            denials = data.get("permission_denials") or []
            if denials:
                for d in denials:
                    self._emit(
                        Event(
                            kind="permission_result",
                            source=self.source,
                            text=str(d.get("message", "") or d),
                            data={"allowed": False, "detail": d},
                        )
                    )
            self._emit(
                Event(
                    kind="result",
                    source=self.source,
                    text=self.run.output,
                    data={
                        "stop_reason": data.get("stop_reason"),
                        "terminal_reason": data.get("terminal_reason"),
                        "cost_usd": self.run.cost_usd,
                        "permission_denials": len(denials),
                    },
                )
            )
        else:
            self._emit(Event(kind="raw", source=self.source, text=json.dumps(data, ensure_ascii=False)[:2000]))
