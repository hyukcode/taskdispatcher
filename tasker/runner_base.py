"""Runner 公共生命周期与事件协议。

具体执行器只需要实现传输层差异：启动、事件泵、消息发送和关闭。
这里使用 Template Method 统一编排生命周期，避免 Claude SDK 与 Codex
App Server 的行为在边界条件上逐渐分叉。
"""

from __future__ import annotations

import queue
import threading
import time
from abc import ABC, abstractmethod
from typing import Callable, Optional

from .approvals import ApprovalBroker
from .config import Config
from .models import Event, TaskLoop, TaskRun


EventSink = Callable[[Optional[TaskRun], Event], None]
_INPUT_STOP = object()
_EVENT_STOP = object()


class RunnerBase(ABC):
    """所有 code-agent runner 的统一外壳。

    子类通过四个钩子接入具体协议：

    * ``_prepare_start``：在线程启动前创建进程/校验 SDK；
    * ``_run_transport``：运行事件泵；
    * ``_take_input_nowait``：由具体 transport 消费中途注入；
    * ``_finalize_transport`` / ``_stop_transport``：优雅关闭与强制停止。

    其余逻辑由本类统一处理，形成 Template Method。
    """

    source = "runner"
    config_key = "claude"
    self_handles_approval = True

    def __init__(
        self,
        cfg: Config,
        run: TaskRun,
        workdir: str,
        on_event: EventSink,
        prompt: str,
        broker: ApprovalBroker | None = None,
    ):
        self.cfg = cfg
        self.run = run
        self.workdir = workdir
        self.on_event = on_event
        self.prompt = prompt
        self.broker = broker or ApprovalBroker(cfg.approval)

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._input_queue: queue.Queue = queue.Queue()
        self._input_lock = threading.Lock()
        self._input_closed = False
        self._event_queue: queue.Queue = queue.Queue()
        self._event_thread: threading.Thread | None = None
        self._event_close_lock = threading.Lock()
        self._event_order_lock = threading.Lock()
        self._event_queue_closed = False
        self._event_sequence = 0
        self._interactions: list[str] = []
        self._result_ts: float | None = None
        self._injection_ts: float | None = None
        self._last_event_ts = time.time()
        section = getattr(cfg, self.config_key, None)
        self._settle = float(getattr(section, "completion_idle", 5.0))

        self._internal_loop: TaskLoop | None = None
        self._loop_iteration = 0

    # ---------- Template Method：公共生命周期 ----------
    def set_internal_loop(self, loop: TaskLoop | None) -> None:
        """配置任务内部 loop；必须在 ``start`` 前调用。"""
        self._internal_loop = loop if loop and loop.enabled else None
        if self._internal_loop is not None:
            self._internal_loop.max_iterations = max(1, int(self._internal_loop.max_iterations))

    def start(self):
        if self._thread is not None:
            raise RuntimeError(f"{self.source} runner 不能重复启动")
        self._prepare_start()
        self.run.started_at = time.time()
        self.run.status = "running"
        self._start_event_dispatcher()
        self._thread = threading.Thread(
            target=self._run_guarded,
            daemon=True,
            name=f"{self.source}-runner-{self.run.task.id}",
        )
        self._thread.start()
        return self._start_result()

    def _prepare_start(self) -> None:
        """在线程启动前准备协议资源。"""

    def _start_result(self):
        return None

    def _start_event_dispatcher(self) -> None:
        self._event_thread = threading.Thread(
            target=self._dispatch_events,
            daemon=True,
            name=f"events-{self.source}-{self.run.task.id}",
        )
        self._event_thread.start()

    def _dispatch_events(self) -> None:
        while True:
            event = self._event_queue.get()
            if event is _EVENT_STOP:
                return
            try:
                self.run.events.append(event)
                self.on_event(self.run, event)
            except Exception:
                # 展示/持久化失败不应反向杀死 agent 的协议线程。
                pass

    @abstractmethod
    def _run_transport(self) -> None:
        """运行具体协议的事件泵。"""

    def _run_guarded(self) -> None:
        try:
            self._run_transport()
        except Exception as exc:  # noqa: BLE001
            self._finish_failure(f"{self.source} runner 异常: {exc}")
        finally:
            self.run.ended_at = time.time()
            self._closed = True
            try:
                self._on_transport_end()
            finally:
                self._close_event_queue()

    def _on_transport_end(self) -> None:
        if self.run.exit_code not in (None, 0) and self.run.status != "failed":
            self._finish_failure(self.run.error or f"{self.source} runner 以错误码结束")
            return
        if self.run.exit_code is None and self.run.status not in ("failed", "success"):
            self._finish_failure(f"{self.source} runner 未返回完成结果")

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def is_done(self) -> bool:
        if self._closed:
            return True
        if self._thread is None:
            return False
        if not self.is_alive():
            return self.run.exit_code is not None or self._stop.is_set()
        if self._result_ts is None:
            return False
        ref = max(self._result_ts, self._injection_ts or 0)
        now = time.time()
        return (now - ref) > self._settle and (now - self._last_event_ts) > self._settle

    def finalize(self) -> None:
        """请求优雅结束具体传输。"""
        self._stop.set()
        self._close_input_queue()
        try:
            self._finalize_transport()
        except Exception:
            pass
        self._join_thread()
        self._close_event_queue()
        self._join_event_thread()

    def _finalize_transport(self) -> None:
        self._stop_transport()

    def stop(self) -> None:
        self._stop.set()
        self._close_input_queue()
        try:
            self._stop_transport()
        except Exception:
            pass
        self._join_thread()
        self._close_event_queue()
        self._join_event_thread()

    def _join_thread(self, timeout: float = 2.0) -> None:
        thread = self._thread
        if thread and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=timeout)

    def _join_event_thread(self, timeout: float = 2.0) -> None:
        thread = self._event_thread
        if thread and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=timeout)

    def _close_input_queue(self) -> None:
        with self._input_lock:
            if self._input_closed:
                return
            self._input_closed = True
            self._input_queue.put(_INPUT_STOP)

    def _take_input_nowait(self) -> tuple[bool, str | None]:
        """取一条待发送消息，返回 ``(是否取到, 消息)``。``None`` 表示关闭。"""
        with self._input_lock:
            try:
                item = self._input_queue.get_nowait()
            except queue.Empty:
                return False, None
        if item is _INPUT_STOP:
            return True, None
        return True, str(item)

    def _close_event_queue(self) -> None:
        with self._event_close_lock:
            if self._event_queue_closed:
                return
            self._event_queue_closed = True
            self._event_queue.put(_EVENT_STOP)

    def _stop_transport(self) -> None:
        """强制停止具体传输；默认行为仅设置 stop event。"""

    # ---------- 公共消息/审批/事件接口 ----------
    def send_message(self, text: str) -> bool:
        if (
            not text
            or self._stop.is_set()
            or self._closed
            or self.run.status == "failed"
            or not self._can_accept_message()
        ):
            return False
        with self._input_lock:
            if self._input_closed or self._stop.is_set() or self._closed:
                return False
            # 先入事件队列，再让 transport 看见消息，保证 user_message
            # 不会排在由该消息触发的 agent 输出之后。
            self._interactions.append(text)
            self._injection_ts = time.time()
            self._emit(Event(kind="user_message", source=self.source, text=text, data={"queued": True}))
            try:
                self._input_queue.put_nowait(text)
            except Exception as exc:  # noqa: BLE001
                self._emit(Event(kind="error", source=self.source, text=f"注入消息失败: {exc}"))
                return False
        return True

    def _can_accept_message(self) -> bool:
        """当前 runner 是否还可以接收待发送消息。"""
        return bool(self._thread and self._thread.is_alive())

    @property
    def pending_approval_ids(self) -> list[str]:
        return self.broker.pending_ids

    def approval_respond(self, req_id: str, allowed: bool) -> bool:
        return self.broker.resolve(req_id, allowed=allowed)

    def _emit(self, event: Event) -> None:
        self._last_event_ts = time.time()
        with self._event_order_lock:
            with self._event_close_lock:
                if self._event_queue_closed:
                    return
                self._event_sequence += 1
                if event.data is None:
                    event.data = {}
                event.data.setdefault("sequence", self._event_sequence)
                self._event_queue.put(event)

    def _finish_failure(self, message: str) -> None:
        if self.run.status == "failed" and self.run.error == message:
            return
        self.run.exit_code = 1
        self.run.status = "failed"
        self.run.error = message
        self.run.output = message
        self._result_ts = time.time()
        self._emit(Event(kind="error", source=self.source, text=message))
