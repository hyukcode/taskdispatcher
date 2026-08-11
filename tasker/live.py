"""实时终端：精简输出 + 后台输入。

输出精简策略：
- 思维链：1 行摘要（max 120 字），dim 色
- 文本输出：最多 3 行，超出折叠
- 工具调用：1 行，输入参数截断到 100 字
- 工具结果：最多 3 行，超出折叠
- 审批请求：醒目横幅，且在此期间暂停其他 runner 的非关键输出
"""

from __future__ import annotations

import queue
import re
import sys
import threading

from . import console
from .models import Event, TaskRun

CMDS = ("help", "status", "plan", "allow", "deny", "pause", "resume", "done", "quit", "q", "exit", "attach")

HELP = """\
输入指令（agent 运行期间随时可用）：

  @all <消息>          发送给当前所有运行中的 agent
  @claude <消息>       只发给 claude 执行的任务
  @codex <消息>        只发给 codex 执行的任务
  @<任务id> <消息>     发给指定任务（如 @t2 ...）
  <普通文本>            等同 @all <文本>，用于继续提要求
  :allow [<id>]        批准最近/指定审批请求
  :deny  [<id>]        拒绝最近/指定审批请求
  :done  [<taskid>]    手动收尾指定/最近任务
  :status              打印各任务进度
  :plan                重新打印计划
  :pause / :resume     暂停输入转发 / 恢复
  :attach <claude|codex>  把终端接管给该 agent 的交互 TUI（macOS 生效）
  :quit                终止所有运行中的 agent 并退出
"""

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
    if s.startswith("@"):
        m = re.match(r"@(\S+)(?:\s+(.*))?$", s)
        target = (m.group(1) or "all").lower() if m else "all"
        text = (m.group(2) or "").strip() if m else ""
        return {"type": "msg", "target": target, "text": text}
    return {"type": "msg", "target": "all", "text": s}


class LiveTui:
    """精简实时输出 + 审批期间暂停非关键事件。"""

    def __init__(self, think_level: str = "full", input_enabled: bool = True):
        self.think_level = think_level
        self.input_enabled = input_enabled
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

    # ---------- 输入 ----------
    def start(self) -> None:
        if not self.input_enabled or self._started:
            return
        self._started = True
        self._input_thread = threading.Thread(target=self._read_loop, daemon=True, name="tui-input")
        self._input_thread.start()
        self.print_raw(console.paint("▶ 输入 :help 查看指令。审批时用 :allow / :deny 决定。", "dim"))

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                break
            if not line:
                break
            line = line.rstrip("\n")
            if not line.strip():
                continue
            if not self._paused:
                self._cmds.put(parse_input_line(line))
            else:
                self.print_raw(console.paint("（输入转发已暂停，:resume 恢复）", "dim"))

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
            try:
                sys.stdout.write(text + "\n")
                sys.stdout.flush()
            except Exception:
                pass

    def emit(self, run: TaskRun, event: Event, source_tag: str = "") -> None:
        """精简输出。"""
        tag = source_tag or event.source
        kind = event.kind

        # 审批期间：只放行审批相关事件和错误
        if self._hold.is_set() and kind not in (
            "permission_request", "permission_result", "error", "result", "user_message"
        ):
            return

        with self._print_lock:
            self._event_counts[kind] = self._event_counts.get(kind, 0) + 1

            if kind == "thinking":
                self._emit_thinking(tag, event)
            elif kind == "tool_use":
                self._emit_tool_use(tag, event)
            elif kind == "tool_result":
                self._emit_tool_result(tag, event)
            elif kind == "permission_request":
                self._emit_permission_request(tag, event)
            elif kind == "permission_result":
                self._emit_permission_result(tag, event)
            elif kind == "text":
                self._emit_text(tag, event)
            elif kind == "user_message":
                self.print_raw(console.paint(f"[{tag}] 👤 {event.text}", "cyan"))
            elif kind == "error":
                self.print_raw(console.paint(f"[{tag}] ❌ {event.text}", "red"))
            elif kind == "result":
                self.print_raw(console.paint(f"[{tag}] 🏁 {_truncate(event.text, 200)}", "green"))
            elif kind == "interaction":
                self.print_raw(console.paint(f"[{tag}] 🔁 {event.text}", "magenta"))
            elif kind == "system":
                self.print_raw(console.paint(f"[{tag}] ⚙ {event.text}", "dim"))
            else:
                self.print_raw(f"[{tag}] [{kind}] {event.text[:120]}")

    # ---- 各事件精简输出 ----
    def _emit_thinking(self, tag: str, event: Event) -> None:
        if self.think_level == "off":
            return
        text = _oneliner(event.text)
        if self.think_level == "head":
            text = _truncate(text, 200)
        else:
            text = _truncate(text, 120)
        self.print_raw(console.paint(f"[{tag}] 💭 {text}", "dim"))

    def _emit_tool_use(self, tag: str, event: Event) -> None:
        name = event.data.get("tool", event.data.get("name", "?"))
        inp = event.data.get("input", {})
        summary = _fmt_tool_input_compact(inp)
        self.print_raw(f"[{tag}] 🔧 {name}  {summary}")

    def _emit_tool_result(self, tag: str, event: Event) -> None:
        is_err = event.data.get("is_error", False)
        icon = "❌" if is_err else "📥"
        text = _truncate(_oneliner(event.text), 200)
        line = f"[{tag}] {icon} {text}"
        self.print_raw(console.paint(line, "red") if is_err else line)

    def _emit_text(self, tag: str, event: Event) -> None:
        lines = (event.text or "").strip().splitlines()
        if not lines:
            return
        shown = lines[:3]
        for ln in shown:
            self.print_raw(f"[{tag}] 💬 {ln[:200]}")
        if len(lines) > 3:
            self.print_raw(console.paint(f"         …（共 {len(lines)} 行，完整内容见 raw 日志）", "dim"))

    def _emit_permission_request(self, tag: str, event: Event) -> None:
        tool = event.data.get("tool", "?")
        inp = event.data.get("input", {})
        req_id = event.data.get("id", "")[:16]
        self.print_raw(console.paint(f"[{tag}] 🛡️ 审批  {tool}", "bold", "yellow"))
        self.print_raw(f"         id={req_id}  {_fmt_tool_input_compact(inp)}")

    def _emit_permission_result(self, tag: str, event: Event) -> None:
        allowed = event.data.get("allowed")
        if allowed is True:
            self.print_raw(console.paint(f"[{tag}] ✅ 已批准", "green"))
        elif allowed is False:
            self.print_raw(console.paint(f"[{tag}] ⛔ 已拒绝", "red"))
        else:
            self.print_raw(f"[{tag}] ❔ {event.text[:120]}")

    # ---------- 控制 ----------
    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._stop.set()
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
