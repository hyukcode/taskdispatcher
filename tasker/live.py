"""实时终端：边跑边把两个 agent 的思维链/工具调用/交互/审批请求打出来，
并开启后台 stdin 读取线程，让用户随时注入消息或下达指令。
"""
from __future__ import annotations

import queue
import re
import sys
import threading

from . import console
from .models import Event, TaskRun

# 允许的 : 指令
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
  :done  [<taskid>]    手动收尾指定/最近任务（关闭其 stdin 让进程退出）
  :status              打印各任务进度
  :plan                重新打印计划
  :pause / :resume     暂停输入转发 / 恢复（不暂停子进程）
  :attach <claude|codex>  把终端接管给该 agent 的交互 TUI（macOS 生效）
  :quit                终止所有运行中的 agent 并退出
"""


def parse_input_line(raw: str) -> dict:
    """把一行用户输入解析为命令字典。"""
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
    """实时输出 + 后台输入。所有打印走统一锁，避免多线程串行输出被搅乱。"""

    def __init__(self, think_level: str = "full", input_enabled: bool = True):
        self.think_level = think_level  # full | head | off
        self.input_enabled = input_enabled
        self._cmds: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        # RLock：emit 持锁时内部会再调用 print_raw（同样加锁），需可重入
        self._print_lock = threading.RLock()
        self._input_thread: threading.Thread | None = None
        self._started = False
        self._paused = False

    # ---------- 输入 ----------
    def start(self) -> None:
        if not self.input_enabled or self._started:
            return
        self._started = True
        self._input_thread = threading.Thread(target=self._read_loop, daemon=True, name="tui-input")
        self._input_thread.start()
        self.print_raw(console.paint("▶ 随时输入消息（@claude/@codex/@all <消息> 或裸文本），:help 查看指令", "dim"))

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

    # ---------- 输出 ----------
    def print_raw(self, text: str) -> None:
        with self._print_lock:
            try:
                sys.stdout.write(text + "\n")
                sys.stdout.flush()
            except Exception:
                pass

    def emit(self, run: TaskRun, event: Event, source_tag: str = "") -> None:
        """主线程/runner 线程都调用它来打印一个事件。"""
        with self._print_lock:
            tag = source_tag or event.source
            if event.kind == "thinking":
                if self.think_level == "off":
                    return
                text = event.text
                if self.think_level == "head" and len(text) > 400:
                    text = text[:400] + "\n…（思考过长，已截断，完整内容见 raw 日志）"
                self._print_blocks(f"[{tag}] 🧠 思考", text, console.paint)
            elif event.kind == "tool_use":
                name = event.data.get("tool", event.data.get("name", "?"))
                inp = event.data.get("input", {})
                self._print_blocks(f"[{tag}] 🔧 工具调用 {name}", _fmt_tool_input(inp), console.paint)
            elif event.kind == "tool_result":
                self._print_blocks(f"[{tag}] 📥 工具结果", event.text, console.paint)
            elif event.kind == "permission_request":
                self.print_raw(console.paint(f"[{tag}] 🛡️ 审批请求：{event.text}", "bold", "yellow"))
                for k, v in event.data.items():
                    if k in ("tool", "input"):
                        self.print_raw(f"         {k}: {_fmt_tool_input(v) if k=='input' else v}")
                self.print_raw(console.paint("         → 输入 :allow 或 :deny 做出决定（headless 下可能已由权限策略自动处理）", "dim"))
            elif event.kind == "permission_result":
                allowed = event.data.get("allowed")
                head = "✅ 已批准" if allowed is True else ("⛔ 已拒绝" if allowed is False else "❔ 审批结果")
                self.print_raw(f"[{tag}] {head}：{event.text}")
            elif event.kind == "user_message":
                self.print_raw(console.paint(f"[{tag}] 👤 注入消息：{event.text}", "cyan"))
            elif event.kind == "interaction":
                self.print_raw(console.paint(f"[{tag}] 🔁 {event.text}", "magenta"))
            elif event.kind == "error":
                self.print_raw(console.paint(f"[{tag}] ❌ {event.text}", "red"))
            elif event.kind == "result":
                self.print_raw(console.paint(f"[{tag}] 🏁 结果：{event.text[:500]}" + ("…" if len(event.text) > 500 else ""), "green"))
            elif event.kind == "text":
                self._print_blocks(f"[{tag}] 💬", event.text, console.paint)
            elif event.kind == "system":
                self.print_raw(console.paint(f"[{tag}] ⚙ {event.text}", "dim"))
            else:
                self.print_raw(f"[{tag}] [{event.kind}] {event.text}")

    @staticmethod
    def _print_blocks(prefix: str, text: str, color) -> None:
        """按行打印，超过 60 行的内容折叠尾部。每次打印后即时 flush，
        保证重定向/管道时事件也是实时出现。"""
        text = (text or "").strip()
        if not text:
            return
        lines = text.splitlines()
        cap = 60
        if len(lines) > cap:
            lines = lines[:cap] + [f"…（共 {len(text.splitlines())} 行，已折叠，完整见 raw 日志）"]
        for ln in lines:
            print(f"{prefix} │ {ln}", flush=True)

    # ---------- 控制 ----------
    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._stop.set()
        if self._input_thread and self._input_thread.is_alive():
            self._input_thread.join(timeout=1.0)


def _fmt_tool_input(inp) -> str:
    import json

    if isinstance(inp, str):
        return inp if len(inp) <= 4000 else inp[:4000] + "\n…（已截断）"
    try:
        s = json.dumps(inp, ensure_ascii=False, indent=2, default=str)
    except Exception:
        s = str(inp)
    return s if len(s) <= 4000 else s[:4000] + "\n…（已截断）"
