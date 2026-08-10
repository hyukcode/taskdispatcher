"""快速测试 codex app-server runner 的协议握手和审批流程。

用法: python scripts/test_app_server.py
需要: codex CLI 已安装且已登录 (codex --version)
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 修复 Windows GBK 编码问题（同 tasker/console.py）
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from tasker.codex_app_server_runner import CodexAppServerRunner
from tasker.config import Config
from tasker.models import Event, TaskRun, SubTask


def main():
    cfg = Config()
    cfg.approval.mode = "ask_console"  # 需要手动 :allow
    cfg.approval.default_allow = True
    cfg.approval.timeout = 15.0  # 15s 超时
    cfg.codex.completion_idle = 3.0
    # untrusted 策略 + workspace-write 沙箱 → 访问工作区外的文件会触发审批
    cfg.codex.approval_policy = "untrusted"
    cfg.codex.sandbox = "workspace-write"
    # 用 --config 覆盖？不，app-server 的 sandbox 经 thread/start 参数设置

    workdir = tempfile.mkdtemp(prefix="tasker_test_")
    print(f"[setup] workdir={workdir}")

    task = SubTask(id="t1", title="test app-server", description="simple test", executor="codex")
    run = TaskRun(task=task, workdir=workdir)

    events: list[Event] = []
    raw_lines: list[str] = []

    def on_event(r, event):
        events.append(event)
        kind_icon = {
            "system": "[SYS]", "thinking": "[THK]", "text": "[TXT]",
            "tool_use": "[TOOL]", "tool_result": "[RES]",
            "permission_request": "[PERM?]", "permission_result": "[PERM!]",
            "result": "[DONE]", "error": "[ERR]", "raw": "[RAW]",
            "user_message": "[MSG]",
        }.get(event.kind, "[???]")
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {kind_icon} {event.kind}: {event.text[:150]}")

    prompt = "读取 C:\\Users\\Lenovo\\.codex\\config.toml 文件的前 5 行，写入到当前目录的 config_preview.txt"
    runner = CodexAppServerRunner(cfg, run, workdir, on_event, prompt)

    print(f"\n[setup] starting app-server runner...")
    runner.start()
    print(f"[setup] runner started, waiting...\n")

    # 后台线程模拟 ask_console 下的 :allow 指令
    def auto_approve():
        while runner.is_alive() and not runner.is_done():
            time.sleep(0.5)
            for rid in list(runner.pending_approval_ids):
                print(f"[auto] :allow {rid[:20]}...")
                runner.approval_respond(rid, allowed=True)

    import threading
    thr = threading.Thread(target=auto_approve, daemon=True)
    thr.start()

    try:
        while not runner.is_done():
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n[test] interrupted")
        runner.stop()

    runner.finalize()
    print(f"\n[test] runner finished")

    # 报告
    print(f"\n{'='*50}")
    print(f"Test Results")
    print(f"{'='*50}")
    print(f"exit_code={run.exit_code}, status={run.status}")
    print(f"duration={run.duration:.1f}s")
    print(f"total events: {len(events)}")
    for kind in ("thinking", "text", "tool_use", "tool_result",
                 "permission_request", "permission_result", "result", "error", "system"):
        count = sum(1 for e in events if e.kind == kind)
        if count:
            print(f"  {kind}: {count}")
    if run.output:
        print(f"\noutput:\n{run.output[:500]}")
    if run.error:
        print(f"\nerror: {run.error}")

    # 检查产物
    out_file = os.path.join(workdir, "config_preview.txt")
    if os.path.exists(out_file):
        content = open(out_file).read()
        print(f"\nconfig_preview.txt:\n{content.strip()[:500]}")
    else:
        print(f"\nconfig_preview.txt NOT created")
        if os.path.exists(workdir):
            files = os.listdir(workdir)
            print(f"workdir contents: {files}")

    return 0 if run.exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
