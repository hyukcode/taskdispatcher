

from __future__ import annotations

import os
import queue
import logging
import sys
import threading

from . import console
from .formatting import compact_json
from .models import Event, TaskRun


logger = logging.getLogger(__name__)

HELP = """\
输入指令（agent 运行期间随时可用）：

  @all <消息>          发送给当前所有运行中的 agent
  @claude <消息>       只发给 claude 执行的任务
  @codex <消息>        只发给 codex 执行的任务
  @<任务id> <消息>     发给指定任务（如 @t2 ...）
  <普通文本>            等同 @all <文本>，用于继续提要求
  :allow [<id>]        批准最近/指定工具审批请求
  :deny  [<id>]        拒绝最近/指定工具审批请求
  :approve [<id>]      人工审查点：通过
  :reject <反馈>       人工审查点：驳回（反馈注回上游重跑）
  :status              打印各任务进度、耗时和注入计数
  :plan                重新打印计划
  :stop /stop          中止当前任务并返回 tasker>（可用 /continue 继续）
  :continue            执行中无操作；结束后在 tasker> 下继续会话
  :new / :restart      执行中不可切换；结束后在 tasker> 下使用对应命令
  :pause / :resume     暂停输入转发 / 恢复
  :quit / quit / exit  终止所有运行中的 agent 并退出程序（或 Ctrl+C）
"""

_CORE_KINDS = frozenset(
    {
        "user_message",
        "text",
        "tool_use",
        "tool_result",
        "permission_request",
        "permission_result",
        "review_request",
        "review_result",
        "interaction",
        "error",
        "result",
    }
)
_DETAIL_KINDS = _CORE_KINDS | frozenset({"thinking", "system", "usage"})

APPROVAL_BANNER = """\
╔══════════════════════════════════════════════════════════╗
║  🛡️  审批请求 — 请立即决定                               ║
║                                                        ║
║   输入 :allow  批准该操作                                ║
║   输入 :deny   拒绝该操作                                ║
║   输入 :help   查看全部指令                              ║
╚══════════════════════════════════════════════════════════╝"""


def parse_input_line(raw: str) -> dict:
    s = raw.strip()
    if not s:
        return {"type": "noop"}
    if s.startswith(":"):
        parts = s[1:].split(None, 1)
        cmd = (parts[0] or "").lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        return {"type": "cmd", "cmd": cmd, "arg": arg}
    if s.lower() == "/stop":
        return {"type": "cmd", "cmd": "stop", "arg": ""}
    if s.lower() in ("quit", "q", "exit", "/quit", "/q", "/exit"):
        return {"type": "cmd", "cmd": "quit", "arg": ""}
    if s.startswith("@"):
        parts = s[1:].split(None, 1)
        target = (parts[0] if parts else "all").lower()
        text = parts[1].strip() if len(parts) > 1 else ""
        return {"type": "msg", "target": target, "text": text}
    return {"type": "msg", "target": "all", "text": s}


_raw_buf = b""


class TerminalSession:
    """管理一次交互式终端会话的 raw mode 生命周期。"""

    def __init__(self):
        self._saved_attrs = None
        self._active = False

    @staticmethod
    def available() -> bool:
        try:
            return bool(sys.stdin.isatty() and sys.stdout.isatty())
        except (AttributeError, OSError) as exc:
            logger.debug("检测交互式终端失败: %s", exc, exc_info=True)
            return False

    def start(self) -> bool:
        global _raw_buf
        _raw_buf = b""
        if not self.available():
            return False
        if os.name == "nt":
            return True

        import termios
        import tty

        try:
            fd = sys.stdin.fileno()
            self._saved_attrs = termios.tcgetattr(fd)
            tty.setraw(fd)
            self._active = True
        except (AttributeError, OSError, ValueError) as exc:
            logger.debug("终端 raw mode 初始化失败: %s", exc, exc_info=True)
            self._saved_attrs = None
        return self._active or os.name == "nt"

    def stop(self) -> None:
        if not self._active or self._saved_attrs is None:
            return
        import termios

        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved_attrs)
        except (OSError, ValueError) as exc:
            logger.debug("恢复终端属性失败: %s", exc, exc_info=True)
        finally:
            self._active = False
            self._saved_attrs = None


def _raw_char() -> str:
    if sys.platform == "win32":
        import msvcrt
        if not msvcrt.kbhit():
            return ""
        try:
            ch = msvcrt.getwch()
        except UnicodeDecodeError:
            return ""
        if ch in ("\x00", "\xe0"):
            try:
                msvcrt.getwch()
            except (AttributeError, OSError, UnicodeDecodeError) as exc:
                logger.debug("读取 Windows 终端字符失败: %s", exc, exc_info=True)
            return ""
        return ch
    else:
        return _raw_char_unix()


def _raw_char_unix() -> str:
    global _raw_buf
    import select

    fd = sys.stdin.fileno()
    try:
        if not select.select([fd], [], [], 0.1)[0]:
            return ""
        b = os.read(fd, 1)
    except (OSError, ValueError) as exc:
        logger.debug("读取 Unix 终端字符失败: %s", exc, exc_info=True)
        return ""
    if not b:
        return "\x04"
    _raw_buf += b
    try:
        ch = _raw_buf.decode("utf-8")
        _raw_buf = b""
    except UnicodeDecodeError:
        return ""
    if ch == "\x1b":
        _drain_esc_seq()
        return ""
    return ch


def _drain_esc_seq() -> None:
    import select
    fd = sys.stdin.fileno()
    try:
        while select.select([fd], [], [], 0.02)[0]:
            os.read(fd, 1)
    except (OSError, ValueError) as exc:
        logger.debug("清理终端 escape sequence 失败: %s", exc, exc_info=True)


class LiveTui:
    """精简实时输出 + 输入行锚定底部 + 审批抑制。"""

    def __init__(self, think_level: str = "full", input_enabled: bool = True, display_level: str = "verbose"):
        self.think_level = think_level
        self.input_enabled = input_enabled
        self.display_level = display_level
        self._cmds: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        self._print_lock = threading.RLock()
        self._input_thread: threading.Thread | None = None
        self._started = False
        self._paused = False
        self._hold = threading.Event()
        self._hold.clear()
        self._event_counts: dict[str, int] = {}
        self._input_buf = ""
        self._prompt = "tasker> "
        self._terminal = TerminalSession()
        self._interactive = False
        self._output_hook = None
        self._previous_output_hook = None
        self._renderers = {
            "thinking": self._emit_thinking_locked,
            "tool_use": self._emit_tool_use_locked,
            "tool_result": self._emit_tool_result_locked,
            "permission_request": self._emit_permission_request_locked,
            "permission_result": self._emit_permission_result_locked,
            "review_request": self._emit_review_request_locked,
            "review_result": self._emit_review_result_locked,
            "text": self._emit_text_locked,
            "user_message": self._emit_user_message_locked,
            "error": self._emit_error_locked,
            "result": self._emit_result_locked,
            "interaction": self._emit_interaction_locked,
            "usage": self._emit_usage_locked,
            "system": self._emit_system_locked,
            "raw": self._emit_raw_locked,
        }

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if not self.input_enabled:
            return
        self._interactive = self._terminal.start()
        if not self._interactive:
            return
        self._output_hook = self._console_hook
        self._previous_output_hook = console.set_output_hook(self._output_hook)
        self.print_raw(console.paint("▶ tasker 已连接。输入 :help 查看指令；审批时用 :allow / :deny 决定。", "dim"))
        self._input_thread = threading.Thread(target=self._read_loop, daemon=True, name="tui-input")
        self._input_thread.start()

    def _console_hook(self, text: str) -> None:
        self.print_raw(text)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ch = _raw_char()
            except Exception:
                logger.exception("实时输入线程异常")
                break
            if not ch:
                continue

            with self._print_lock:
                if ch in ("\r", "\n"):
                    self._handle_enter()
                elif ch in ("\x08", "\x7f"):
                    self._handle_backspace()
                elif ch == "\x03":
                    self._input_buf = ""
                    self._redraw()
                    self._cmds.put({"type": "cmd", "cmd": "quit", "arg": ""})
                    self._stop.set()
                    break
                elif ch in ("\x04", "\x1a"):
                    self._cmds.put({"type": "cmd", "cmd": "quit", "arg": ""})
                    self._input_buf = ""
                    self._redraw()
                    self._stop.set()
                    break
                elif ch >= " " or ch == "\t":
                    self._input_buf += ch
                    sys.stdout.write(ch)
                    sys.stdout.flush()

    def _handle_enter(self) -> None:
        line = self._input_buf
        self._input_buf = ""
        sys.stdout.write("\r\033[K")
        sys.stdout.write(f"{self._prompt}{line}\n")
        sys.stdout.flush()
        if line.strip():
            if not self._paused:
                self._cmds.put(parse_input_line(line))
            else:
                self._write_locked(console.paint("（输入转发已暂停，:resume 恢复）", "dim"))
        self._redraw()

    def _handle_backspace(self) -> None:
        if self._input_buf:
            self._input_buf = self._input_buf[:-1]
            sys.stdout.write("\b \b")
            sys.stdout.flush()

    def poll_commands(self, timeout: float = 0.1) -> list[dict]:
        out: list[dict] = []
        try:
            out.append(self._cmds.get(timeout=max(0.0, timeout)))
            while True:
                out.append(self._cmds.get_nowait())
        except queue.Empty:
            pass
        return out

    # ---------- 审批抑制 ----------
    def hold_for_approval(self) -> None:
        with self._print_lock:
            self._hold_for_approval_locked()

    def _hold_for_approval_locked(self) -> None:
        if self._hold.is_set():
            return
        self._hold.set()
        self._write_locked("\n" + console.paint(APPROVAL_BANNER, "bold", "yellow") + "\n")

    def release_hold(self) -> None:
        with self._print_lock:
            self._hold.clear()

    @property
    def is_held(self) -> bool:
        return self._hold.is_set()

    # ---------- 输出 ----------
    def print_raw(self, text: str) -> None:
        with self._print_lock:
            self._write_locked(text)

    def _write_locked(self, text: str) -> None:
        sys.stdout.write("\r\033[K")
        sys.stdout.write(text + "\n")
        self._redraw()

    def _redraw(self) -> None:
        sys.stdout.write(f"\r{self._prompt}{self._input_buf}")
        sys.stdout.flush()

    def emit(self, run: TaskRun | None, event: Event, source_tag: str = "") -> None:
        tag = self._event_tag(run, event, source_tag)
        kind = event.kind

        if self._hold.is_set() and kind not in (
            "permission_request", "permission_result", "review_request", "review_result", "error", "result", "user_message"
        ):
            return

        if self.display_level == "debug":
            visible = True
        elif self.display_level == "verbose":
            visible = kind in _DETAIL_KINDS
        else:
            visible = kind in _CORE_KINDS or (
                kind == "system" and bool(event.data.get("display", False))
            )
        if not visible:
            return

        if not self._interactive:
            with self._print_lock:
                self._event_counts[kind] = self._event_counts.get(kind, 0) + 1
                console.event_line(event.summary(), source=tag)
            return

        with self._print_lock:
            self._event_counts[kind] = self._event_counts.get(kind, 0) + 1
            if kind in ("permission_request", "review_request") and not event.data.get("auto"):
                self._hold_for_approval_locked()
            elif kind in ("permission_result", "review_result"):
                self._hold.clear()
            renderer = self._renderers.get(kind, self._emit_generic_locked)
            renderer(tag, event)

    @staticmethod
    def _event_tag(run: TaskRun | None, event: Event, source_tag: str = "") -> str:
        """让多 agent 输出具备 CLI 风格的稳定上下文标签。"""
        if source_tag:
            return source_tag
        if run is not None:
            task_id = getattr(run.task, "id", "?")
            executor = getattr(run.task, "executor", event.source)
            return f"{task_id} · {executor}"
        return event.source

    def _emit_user_message_locked(self, tag: str, event: Event) -> None:
        self._write_locked(console.paint(f"[{tag}] 👤 {event.text}", "cyan"))

    def _emit_error_locked(self, tag: str, event: Event) -> None:
        self._write_locked(console.paint(f"[{tag}] ❌ {event.text}", "red"))

    def _emit_result_locked(self, tag: str, event: Event) -> None:
        self._write_locked(console.paint(f"[{tag}] 🏁 {_truncate(event.text, 200)}", "green"))

    def _emit_interaction_locked(self, tag: str, event: Event) -> None:
        self._write_locked(console.paint(f"[{tag}] 🔁 {event.text}", "magenta"))

    def _emit_system_locked(self, tag: str, event: Event) -> None:
        self._write_locked(console.paint(f"[{tag}] ⚙ {event.text}", "dim"))

    def _emit_raw_locked(self, tag: str, event: Event) -> None:
        self._write_locked(console.paint(f"[{tag}] 🧪 {event.text[:120]}", "dim"))

    def _emit_generic_locked(self, tag: str, event: Event) -> None:
        self._write_locked(f"[{tag}] [{event.kind}] {event.text[:120]}")

    def _emit_thinking_locked(self, tag: str, event: Event) -> None:
        if self.think_level == "off":
            return
        text = _oneliner(event.text)
        if self.think_level == "head":
            text = _truncate(text, 200)
        else:
            text = _truncate(text, 120)
        self._write_locked(console.paint(f"[{tag}] 💭 {text}", "dim"))

    def _emit_tool_use_locked(self, tag: str, event: Event) -> None:
        name = event.data.get("tool", event.data.get("name", "?"))
        inp = event.data.get("input", {})
        summary = _fmt_tool_input_compact(inp)
        self._write_locked(f"[{tag}] 🔧 {name}  {summary}")

    def _emit_tool_result_locked(self, tag: str, event: Event) -> None:
        is_err = event.data.get("is_error", False)
        icon = "❌" if is_err else "📥"
        text = _truncate(_oneliner(event.text), 200)
        line = f"[{tag}] {icon} {text}"
        self._write_locked(console.paint(line, "red") if is_err else line)

    def _emit_usage_locked(self, tag: str, event: Event) -> None:
        data = event.data or {}
        parts = []
        for key, label in (("input_tokens", "in"), ("output_tokens", "out"), ("total_tokens", "total"), ("cost_usd", "cost")):
            value = data.get(key)
            if value is not None:
                parts.append(f"{label}={value}")
        text = event.text or " ".join(parts) or "用量已更新"
        self._write_locked(console.paint(f"[{tag}] 📊 {_truncate(text, 180)}", "dim"))

    def _emit_text_locked(self, tag: str, event: Event) -> None:
        lines = (event.text or "").strip().splitlines()
        if not lines:
            return
        shown = lines[:3]
        for ln in shown:
            self._write_locked(f"[{tag}] 💬 {ln[:200]}")
        if len(lines) > 3:
            self._write_locked(console.paint(f"         …（共 {len(lines)} 行，完整内容见 raw 日志）", "dim"))

    def _emit_permission_request_locked(self, tag: str, event: Event) -> None:
        if event.data.get("auto"):
            return
        tool = event.data.get("tool", "?")
        inp = event.data.get("input", {})
        req_id = event.data.get("id", "")[:16]
        self._write_locked(console.paint(f"[{tag}] 🛡️ 审批  {tool}", "bold", "yellow"))
        self._write_locked(f"         id={req_id}  {_fmt_tool_input_compact(inp)}")

    def _emit_permission_result_locked(self, tag: str, event: Event) -> None:
        if event.data.get("auto"):
            return
        allowed = event.data.get("allowed")
        if allowed is True:
            self._write_locked(console.paint(f"[{tag}] ✅ 已批准", "green"))
        elif allowed is False:
            self._write_locked(console.paint(f"[{tag}] ⛔ 已拒绝", "red"))
        else:
            self._write_locked(f"[{tag}] ❔ {event.text[:120]}")

    def _emit_review_request_locked(self, tag: str, event: Event) -> None:
        node = event.data.get("node", "")
        title = event.data.get("title", "人工审查")
        self._write_locked(console.paint(f"[{tag}] 👁️ 人工审查点 {node} — {title}", "bold", "yellow"))
        self._write_locked("         输入 :approve 通过 | :reject <反馈> 驳回重跑")
        if event.text:
            self._write_locked(f"         {_truncate(event.text, 200)}")

    def _emit_review_result_locked(self, tag: str, event: Event) -> None:
        approved = event.data.get("approved")
        if approved is True:
            self._write_locked(console.paint(f"[{tag}] ✅ 审查通过", "green"))
        elif approved is False:
            self._write_locked(console.paint(f"[{tag}] ↩️ 审查驳回：{_truncate(event.text, 160)}", "yellow"))
        else:
            self._write_locked(f"[{tag}] ❔ 审查结果 {_truncate(event.text, 160)}")

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._stop.set()
        self._terminal.stop()
        if self._output_hook is not None:
            console.restore_output_hook(self._output_hook, self._previous_output_hook)
            self._output_hook = None
            self._previous_output_hook = None
        self._interactive = False
        if self._input_thread and self._input_thread.is_alive():
            self._input_thread.join(timeout=1.0)


def _truncate(text: str, width: int) -> str:
    s = text.strip().replace("\n", " ")
    return s if len(s) <= width else s[:width - 1] + "…"


def _oneliner(text: str) -> str:
    t = text.strip()
    if not t:
        return ""
    first = t.split("\n")[0].strip()
    return first[:200]


def _fmt_tool_input_compact(inp) -> str:
    if isinstance(inp, str):
        return _truncate(inp, 100)
    if isinstance(inp, dict):
        keys = []
        for k in ("command", "file_path", "description", "path", "query", "url", "message", "content", "text"):
            if k in inp:
                v = inp[k]
                if isinstance(v, str):
                    keys.append(f"{k}={_truncate(v, 50)}")
                else:
                    keys.append(f"{k}=…")
                if len(keys) >= 2:
                    break
        if not keys:
            keys = [f"{k}=…" for k in list(inp.keys())[:2]]
        return ", ".join(keys)[:100]
    return _truncate(compact_json(inp, 100), 100)
