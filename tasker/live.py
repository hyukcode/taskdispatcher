"""实时终端：精简输出 + 输入行锚定底部。

输出精简策略：
- 思维链：1 行摘要（max 120 字），dim 色
- 文本输出：最多 3 行，超出折叠
- 工具调用：1 行，输入参数截断到 100 字
- 工具结果：最多 3 行，超出折叠
- 审批请求：醒目横幅，且在此期间暂停其他 runner 的非关键输出

输入锚定：
- 使用逐字符读取（替代 readline），在每次输出前后清除/恢复输入行
- 用户输入始终可见，不被事件流刷屏
"""

from __future__ import annotations

import queue
import re
import sys
import threading

from . import console
from .models import Event, TaskRun

CMDS = ("help", "status", "plan", "allow", "deny", "approve", "reject", "pause", "resume", "done", "quit", "q", "exit", "attach")

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
  :status              打印各任务进度
  :plan                重新打印计划
  :pause / :resume     暂停输入转发 / 恢复
  :quit / quit / exit  终止所有运行中的 agent 并退出程序（或 Ctrl+C）
"""

# minimal 显示只放行这些事件（其余 thinking/tool_use/tool_result/text/system 不打印）
_MINIMAL_KINDS = frozenset(
    {"result", "permission_request", "permission_result", "review_request", "review_result", "error", "user_message"}
)

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
    # 退出词兼容：裸 quit/exit/q 或 /quit /exit /q 在运行中也当作退出指令（不再误发成消息）
    if s.lower() in ("quit", "q", "exit", "/quit", "/q", "/exit"):
        return {"type": "cmd", "cmd": "quit", "arg": ""}
    if s.startswith("@"):
        m = re.match(r"@(\S+)(?:\s+(.*))?$", s)
        target = (m.group(1) or "all").lower() if m else "all"
        text = (m.group(2) or "").strip() if m else ""
        return {"type": "msg", "target": target, "text": text}
    return {"type": "msg", "target": "all", "text": s}


# ================================================================
#  平台适配：逐字符输入
# ================================================================
_raw_buf = b""  # Unix: 多字节 UTF-8 累积缓冲区


def _raw_char() -> str:
    """读取单个 Unicode 字符（阻塞）。跨平台实现。

    Windows: msvcrt.getwch() 直接返回宽字符。
    macOS/Linux: os.read(fd, 1) 逐字节读，累积到 _raw_buf 后解码为完整字符。
                 方向键等 ESC 序列被吞掉。
    """
    if sys.platform == "win32":
        import msvcrt
        try:
            ch = msvcrt.getwch()
        except UnicodeDecodeError:
            return ""
        # 特殊键前缀 → 吞掉后续扫描码
        if ch in ("\x00", "\xe0"):
            try:
                msvcrt.getwch()
            except Exception:
                pass
            return ""
        return ch
    else:
        return _raw_char_unix()


def _raw_char_unix() -> str:
    """Unix 逐字节读取 + UTF-8 解码 + ESC 序列吞掉。"""
    global _raw_buf
    import os
    fd = sys.stdin.fileno()
    try:
        b = os.read(fd, 1)
    except Exception:
        return ""
    if not b:
        return ""  # EOF
    _raw_buf += b
    try:
        ch = _raw_buf.decode("utf-8")
        _raw_buf = b""
    except UnicodeDecodeError:
        return ""  # 多字节字符尚未完整
    if ch == "\x1b":  # ESC 序列（方向键等）
        _drain_esc_seq()
        return ""
    return ch


def _drain_esc_seq() -> None:
    """吞掉 ESC 后续字节，避免方向键等输出乱码。"""
    import os
    import select
    fd = sys.stdin.fileno()
    try:
        while select.select([fd], [], [], 0.02)[0]:
            os.read(fd, 1)
    except Exception:
        pass


def _raw_start() -> None:
    """进入原始模式（macOS/Linux only）。"""
    global _raw_buf
    _raw_buf = b""  # 清空累积的字节缓冲
    if sys.platform == "win32":
        return
    if not sys.stdin.isatty():
        return
    import atexit
    import termios
    import tty
    fd = sys.stdin.fileno()
    try:
        _raw_start._saved = termios.tcgetattr(fd)  # type: ignore[attr-defined]
    except Exception:
        _raw_start._saved = None  # type: ignore[attr-defined]
    if _raw_start._saved:  # type: ignore[attr-defined]
        tty.setraw(fd)
        atexit.register(_raw_stop)


def _raw_stop() -> None:
    """恢复终端设置（Unix only）。"""
    if sys.platform == "win32":
        return
    import termios
    saved = getattr(_raw_start, "_saved", None)
    if saved:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
        except Exception:
            pass


# ================================================================
#  LiveTui
# ================================================================
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
        # 审批期间抑制其他 runner 的非关键输出
        self._hold = threading.Event()
        self._hold.clear()
        # 统计
        self._event_counts: dict[str, int] = {}
        # 输入缓冲（逐字符模式）
        self._input_buf = ""
        self._prompt = "> "

    # ---------- 输入 ----------
    def start(self) -> None:
        if not self.input_enabled or self._started:
            return
        self._started = True
        _raw_start()
        # 注册 console 输出钩子，让所有 console.* / print 也走输入行恢复
        console._write_hook = self._console_hook
        # 打印启动提示（作为普通输出，不干扰输入行）
        self.print_raw(console.paint("▶ 输入 :help 查看指令。审批时用 :allow / :deny 决定。", "dim"))
        self._input_thread = threading.Thread(target=self._read_loop, daemon=True, name="tui-input")
        self._input_thread.start()

    def _console_hook(self, text: str) -> None:
        """console._out → 统一经 print_raw 输出，避免绕过输入行恢复。"""
        self.print_raw(text)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ch = _raw_char()
            except Exception:
                break
            if not ch:
                continue

            with self._print_lock:
                if ch in ("\r", "\n"):
                    self._handle_enter()
                elif ch in ("\x08", "\x7f"):  # Backspace
                    self._handle_backspace()
                elif ch == "\x03":  # Ctrl+C
                    self._input_buf = ""
                    self._redraw()
                    self._cmds.put({"type": "cmd", "cmd": "quit", "arg": ""})
                    self._stop.set()
                    break
                elif ch in ("\x04", "\x1a"):  # Ctrl+D (EOF) / Ctrl+Z (Windows EOF)
                    self._cmds.put({"type": "cmd", "cmd": "quit", "arg": ""})
                    self._input_buf = ""
                    self._redraw()
                    break
                elif ch >= " " or ch == "\t":
                    self._input_buf += ch
                    sys.stdout.write(ch)
                    sys.stdout.flush()

    def _handle_enter(self) -> None:
        """Enter 键：提交当前缓冲。"""
        line = self._input_buf
        self._input_buf = ""
        # 清除当前行
        sys.stdout.write("\r\033[K")
        # 回显提交的内容
        sys.stdout.write(f"{self._prompt}{line}\n")
        sys.stdout.flush()
        if line.strip():
            if not self._paused:
                self._cmds.put(parse_input_line(line))
            else:
                self._write_locked(console.paint("（输入转发已暂停，:resume 恢复）", "dim"))
        self._redraw()

    def _handle_backspace(self) -> None:
        """退格键。"""
        if self._input_buf:
            self._input_buf = self._input_buf[:-1]
            sys.stdout.write("\b \b")
            sys.stdout.flush()

    def poll_commands(self, timeout: float = 0.1) -> list[dict]:
        out: list[dict] = []
        try:
            while True:
                out.append(self._cmds.get_nowait())
        except queue.Empty:
            pass
        return out

    # ---------- 审批抑制 ----------
    def hold_for_approval(self) -> None:
        """审批出现：暂停其他 runner 的非关键输出，并打印醒目横幅。"""
        if not self._hold.is_set():
            self._hold.set()
            self.print_raw("")
            self.print_raw(console.paint(APPROVAL_BANNER, "bold", "yellow"))
            self.print_raw("")

    def release_hold(self) -> None:
        """审批结束：恢复输出。"""
        self._hold.clear()

    @property
    def is_held(self) -> bool:
        return self._hold.is_set()

    # ---------- 输出 ----------
    def print_raw(self, text: str) -> None:
        with self._print_lock:
            self._write_locked(text)

    def _write_locked(self, text: str) -> None:
        """输出一行文本（调用方须已持有 _print_lock）。

        步骤：清除输入行 → 打印输出 → 恢复输入行。
        """
        # 清除当前输入行
        sys.stdout.write("\r\033[K")
        # 输出内容
        sys.stdout.write(text + "\n")
        # 恢复输入行
        self._redraw()

    def _redraw(self) -> None:
        """重绘输入提示行。"""
        sys.stdout.write(f"\r{self._prompt}{self._input_buf}")
        sys.stdout.flush()

    def emit(self, run: TaskRun, event: Event, source_tag: str = "") -> None:
        """精简输出。"""
        tag = source_tag or event.source
        kind = event.kind

        # 审批期间：只放行审批相关事件和错误
        if self._hold.is_set() and kind not in (
            "permission_request", "permission_result", "review_request", "review_result", "error", "result", "user_message"
        ):
            return

        # minimal 显示：只放行关键事件（result / 审批 / 审查 / 错误），其余不打印
        if self.display_level == "minimal" and kind not in _MINIMAL_KINDS:
            return

        with self._print_lock:
            self._event_counts[kind] = self._event_counts.get(kind, 0) + 1

            if kind == "thinking":
                self._emit_thinking_locked(tag, event)
            elif kind == "tool_use":
                self._emit_tool_use_locked(tag, event)
            elif kind == "tool_result":
                self._emit_tool_result_locked(tag, event)
            elif kind == "permission_request":
                self._emit_permission_request_locked(tag, event)
            elif kind == "permission_result":
                self._emit_permission_result_locked(tag, event)
            elif kind == "review_request":
                self._emit_review_request_locked(tag, event)
            elif kind == "review_result":
                self._emit_review_result_locked(tag, event)
            elif kind == "text":
                self._emit_text_locked(tag, event)
            elif kind == "user_message":
                self._write_locked(console.paint(f"[{tag}] 👤 {event.text}", "cyan"))
            elif kind == "error":
                self._write_locked(console.paint(f"[{tag}] ❌ {event.text}", "red"))
            elif kind == "result":
                self._write_locked(console.paint(f"[{tag}] 🏁 {_truncate(event.text, 200)}", "green"))
            elif kind == "interaction":
                self._write_locked(console.paint(f"[{tag}] 🔁 {event.text}", "magenta"))
            elif kind == "system":
                self._write_locked(console.paint(f"[{tag}] ⚙ {event.text}", "dim"))
            else:
                self._write_locked(f"[{tag}] [{kind}] {event.text[:120]}")

    # ---- 各事件精简输出（调用方已持有 _print_lock） ----
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
            return  # 自动通过的审批不展示
        tool = event.data.get("tool", "?")
        inp = event.data.get("input", {})
        req_id = event.data.get("id", "")[:16]
        self._write_locked(console.paint(f"[{tag}] 🛡️ 审批  {tool}", "bold", "yellow"))
        self._write_locked(f"         id={req_id}  {_fmt_tool_input_compact(inp)}")

    def _emit_permission_result_locked(self, tag: str, event: Event) -> None:
        if event.data.get("auto"):
            return  # 自动通过的审批不展示
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

    # ---------- 控制 ----------
    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._stop.set()
        # 恢复终端设置
        _raw_stop()
        # 注销 console 钩子
        console._write_hook = None
        if self._input_thread and self._input_thread.is_alive():
            self._input_thread.join(timeout=1.0)


# ================================================================
#  辅助
# ================================================================
def _truncate(text: str, width: int) -> str:
    s = text.strip().replace("\n", " ")
    return s if len(s) <= width else s[:width - 1] + "…"


def _oneliner(text: str) -> str:
    """取首行（或前 80 字）。"""
    t = text.strip()
    if not t:
        return ""
    first = t.split("\n")[0].strip()
    return first[:200]


def _fmt_tool_input_compact(inp) -> str:
    """紧凑工具输入摘要（单行，max 100 字）。"""
    import json

    if isinstance(inp, str):
        return _truncate(inp, 100)
    if isinstance(inp, dict):
        # 摘取最关键的 key
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
    try:
        s = json.dumps(inp, ensure_ascii=False, default=str)
        return _truncate(s, 100)
    except Exception:
        return _truncate(str(inp), 100)


def _fmt_tool_input(inp) -> str:
    """完整工具输入（用于审批请求详情，保留兼容）。"""
    import json

    if isinstance(inp, str):
        return inp if len(inp) <= 4000 else inp[:4000] + "\n…（已截断）"
    try:
        s = json.dumps(inp, ensure_ascii=False, indent=2, default=str)
    except Exception:
        s = str(inp)
    return s if len(s) <= 4000 else s[:4000] + "\n…（已截断）"
