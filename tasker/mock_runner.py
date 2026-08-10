"""模拟 runner：不真的调用 claude/codex，按时间线伪造完整事件流。
用于 --mock 演示（无需 API key / CLI 也能跑通全流程）与测试。
"""
from __future__ import annotations

import threading
import time

from .models import Event, TaskRun


class MockRunner:
    source = "mock"

    def __init__(self, cfg, run: TaskRun, workdir: str, on_event, prompt: str):
        self.cfg = cfg
        self.run = run
        self.workdir = workdir
        self.on_event = on_event
        self.prompt = prompt
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interactions: list[str] = []

    def start(self):
        self.run.started_at = time.time()
        self._thread = threading.Thread(target=self._script, daemon=True, name=f"mock-{self.run.task.id}")
        self._thread.start()

    def send_message(self, text: str) -> bool:
        self._interactions.append(text)
        self._emit(Event(kind="user_message", source=self.source, text=text))
        # 模拟收到注入后继续思考
        self._emit(Event(kind="thinking", source=self.source, text=f"收到用户注入：{text}\n我来据此调整后续动作。"))
        return True

    def stop(self):
        self._stop.set()

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def is_done(self):
        return not self.is_alive()

    def finalize(self):
        pass

    def _emit(self, event: Event):
        self.run.events.append(event)
        self.on_event(self.run, event)

    def _script(self):
        tid = self.run.task.id
        exe = self.run.task.executor
        delay = 0.6
        try:
            self._emit(Event(kind="thinking", source=self.source, text=f"[{exe}] 任务 {tid} 开始。目标：{self.run.task.title}\n我先理清输入、规划步骤，然后逐步执行。"))
            time.sleep(delay)
            if self._stop.is_set():
                return
            self._emit(
                Event(
                    kind="tool_use",
                    source=self.source,
                    text="Read",
                    data={"tool": "Read", "input": {"path": "workspace/README.md"}, "id": "mock_read_1"},
                )
            )
            time.sleep(delay)
            self._emit(
                Event(
                    kind="tool_result",
                    source=self.source,
                    text="# 项目说明\n这是模拟工作区内容。",
                    data={"tool": "Read", "is_error": False},
                )
            )
            self._emit(Event(kind="thinking", source=self.source, text=f"[{exe}] 已读取上下文，接下来执行核心动作。"))

            # 模拟一次审批请求（演示审批事件流；由 ApprovalPolicy auto 模式自动批准并回注消息）
            self._emit(
                Event(
                    kind="permission_request",
                    source=self.source,
                    text="Write",
                    data={"id": f"mock_approve_{tid}", "tool": "Write", "input": {"path": "out.txt", "content": "…"}},
                )
            )
            time.sleep(delay)

            self._emit(
                Event(
                    kind="tool_use",
                    source=self.source,
                    text="Write",
                    data={"tool": "Write", "input": {"path": "out.txt", "content": "模拟产物"}, "id": "mock_write_1"},
                )
            )
            time.sleep(delay)
            self._emit(Event(kind="tool_result", source=self.source, text="已写入 out.txt", data={"tool": "Write", "is_error": False}))
            self._emit(Event(kind="thinking", source=self.source, text=f"[{exe}] 核心动作完成，整理结果。"))
            time.sleep(delay)
            self.run.output = f"任务 {tid}（{exe}）完成：产物已写入工作区。"
            self.run.exit_code = 0
            self.run.cost_usd = 0.01
            self._emit(
                Event(
                    kind="result",
                    source=self.source,
                    text=self.run.output,
                    data={"cost_usd": self.run.cost_usd, "mock": True},
                )
            )
        finally:
            self.run.ended_at = time.time()
