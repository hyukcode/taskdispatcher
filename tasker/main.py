"""CLI 入口。

用法：
  tasker run <prompt>                交互式运行（实时输出思维链/工具/交互/审批，可中途注入消息）
  tasker plan <prompt>                只打印拆分计划
  tasker attach claude|codex [prompt] 把终端交给交互 TUI（macOS 看真实审批）
  tasker verify-config                检查 CLI / API key
  tasker init                         生成 config.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from . import console
from .config import load_config, save_example_config
from .live import LiveTui
from .llm import LLMError
from .planner import plan_with_llm, plan_with_rules
from .scheduler import Scheduler

try:
    from .mock_runner import MockRunner
except ImportError:  # pragma: no cover
    MockRunner = None


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tasker", description="把 prompt 拆给 claude code 与 codex 执行，实时输出两者思维链/工具调用/交互/审批")
    p.add_argument("--version", action="version", version=f"tasker {__version__}")
    p.add_argument("--config", default=None, help="配置文件路径（默认 ./config.json）")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="交互式运行")
    r.add_argument("prompt", nargs="?", default="")
    r.add_argument("--mock", action="store_true", help="用模拟 runner 演示全流程（无需 claude/codex/API key）")
    r.add_argument("--use-sdk", action="store_true", default=True, help="claude 任务使用官方 claude-agent-sdk（默认启用；若未安装会自动降级到 stream-json）")
    r.add_argument("--no-sdk", action="store_true", help="强制使用 stream-json 后端（关闭 SDK）")
    r.add_argument("--no-app-server", action="store_true", help="强制使用 codex exec 后端（关闭 app-server JSON-RPC）")
    r.add_argument("--plan-rules", action="store_true", help="用规则拆分，不调用 LLM")
    r.add_argument("--think", choices=["full", "head", "off"], default="full", help="思维链输出：full 完整（默认）/ head 截断 / off 隐藏")
    r.add_argument("--no-input", action="store_true", help="关闭交互输入（仅流式输出）")
    r.add_argument("--report", action="store_true", help="结束后额外写一份 Markdown 报告")
    r.add_argument("--max-parallel", type=int, default=None)
    r.add_argument("--timeout", type=float, default=None, help="单任务超时秒数")

    pl = sub.add_parser("plan", help="打印拆分计划")
    pl.add_argument("prompt", nargs="?", default="")
    pl.add_argument("--mock", action="store_true")
    pl.add_argument("--plan-rules", action="store_true")
    pl.add_argument("--json", action="store_true", help="以 JSON 输出计划")

    a = sub.add_parser("attach", help="ptty attach 到交互 TUI（macOS/Linux）")
    a.add_argument("tool", choices=["claude", "codex"])
    a.add_argument("prompt", nargs="*", default=[])
    a.add_argument("--workdir", default=None)

    v = sub.add_parser("verify-config", help="检查环境")
    v.add_argument("--json", action="store_true")

    sub.add_parser("init", help="生成 config.json 模板")
    return p


def _read_prompt(arg: str) -> str:
    if arg:
        return arg
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return input("请输入任务目标: ").strip()


def cmd_run(args, cfg) -> int:
    prompt = _read_prompt(args.prompt)
    if not prompt:
        console.error("没有输入 prompt")
        return 2

    overrides = {}
    if args.max_parallel:
        overrides["max_parallel"] = args.max_parallel
    if args.timeout:
        overrides["timeout_per_task"] = args.timeout
    cfg = load_config(args.config, overrides=overrides)

    tui = LiveTui(think_level=args.think, input_enabled=not args.no_input)

    # 计划：mock 或 plan-rules 用规则；否则调 LLM（失败时回退到规则拆分）
    if args.mock:
        cfg.mock = True
        plan = plan_with_rules(prompt)
    elif args.plan_rules:
        plan = plan_with_rules(prompt)
    else:
        try:
            plan = plan_with_llm(prompt, cfg, emit=lambda e: tui.emit(None, e, "planner"))  # type: ignore[arg-type]
        except LLMError as e:
            console.warn(f"任务拆分 LLM 不可用（{e}），回退到规则拆分")
            plan = plan_with_rules(prompt)

    if args.mock:
        _inject_mock_executor(cfg)
    else:
        if not args.no_sdk:
            _inject_sdk_executor()
        if not args.no_app_server:
            _inject_codex_app_server(cfg)

    sched = Scheduler(cfg, prompt, plan, tui)
    runs = sched.run()

    console.banner("汇总")
    total_cost = sum(r.cost_usd for r in runs)
    for r in runs:
        mark = "✓" if r.status == "success" else "✗"
        print(f"  {mark} {r.task.id} [{r.task.executor}] {r.status:<9} {r.duration:6.1f}s  ${r.cost_usd:.4f}")
    print(f"  合计成本: ${total_cost:.4f}")

    if args.report:
        from .report import write_report

        path = write_report(plan, runs, prompt, cfg.report_path)
        console.ok(f"报告已写入 {path}")
    return 0 if all(r.status == "success" for r in runs) else 1


def _inject_mock_executor(cfg) -> None:
    import tasker.scheduler as sched_mod

    if MockRunner is None:
        return
    sched_mod.EXECUTOR_TO_RUNNER = {"claude": MockRunner, "codex": MockRunner}


def _inject_sdk_executor() -> bool:
    """将 claude executor 切换为 SDK 后端。返回 True 表示切换成功。"""
    import tasker.scheduler as sched_mod

    try:
        import claude_agent_sdk  # noqa: F401 — 检测 SDK 是否已安装
    except ImportError:
        console.warn("claude-agent-sdk 未安装，claude 任务将使用 stream-json 后端（pip install claude-agent-sdk 可启用 headless 审批）")
        return False

    from .sdk_runner import SdkClaudeRunner

    sched_mod.EXECUTOR_TO_RUNNER["claude"] = SdkClaudeRunner
    console.info("claude 使用官方 claude-agent-sdk 后端（headless 审批可用）")
    return True


def _inject_codex_app_server(cfg) -> bool:
    """把 codex executor 切换为 app-server JSON-RPC 后端。返回 True 表示切换成功。"""
    if not cfg.codex.use_app_server:
        return False

    import subprocess

    import tasker.scheduler as sched_mod

    from .spawn import resolve_binary

    try:
        r = subprocess.run(
            [resolve_binary(cfg.codex.binary), "app-server", "--help"],
            capture_output=True,
            timeout=10,
        )
        ok = r.returncode == 0
    except Exception:  # noqa: BLE001
        ok = False

    if not ok:
        console.warn("codex app-server 不可用（codex 版本过旧），codex 任务回退到 codex exec 后端")
        return False

    from .codex_app_server_runner import CodexAppServerRunner

    sched_mod.EXECUTOR_TO_RUNNER["codex"] = CodexAppServerRunner
    console.info("codex 使用 app-server JSON-RPC 后端（审批可交互、支持中途注入）")
    return True


def cmd_plan(args, cfg) -> int:
    prompt = _read_prompt(args.prompt)
    if args.mock or args.plan_rules:
        plan = plan_with_rules(prompt)
    else:
        try:
            plan = plan_with_llm(prompt, cfg, emit=lambda e: console.dim(f"[planner] {e.text}"))
        except LLMError as e:
            console.warn(f"任务拆分 LLM 不可用（{e}），回退到规则拆分")
            plan = plan_with_rules(prompt)
    if args.json:
        print(
            json.dumps(
                {
                    "objective": plan.objective,
                    "rationale": plan.rationale,
                    "tasks": [t.__dict__ for t in plan.tasks],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"目标: {plan.objective}")
        if plan.rationale:
            print(f"说明: {plan.rationale}")
        for t in plan.tasks:
            print(f"  {t.id} [{t.executor}] ← {','.join(t.depends_on) or '—'}  {t.title}")
            print(f"      {t.description.replace(chr(10), ' ')[:120]}")
    return 0


def cmd_attach(args, cfg) -> int:
    import tasker.ptty as ptty_mod

    prompt = " ".join(args.prompt)
    workdir = args.workdir or str(cfg.workspace_path)
    os.makedirs(workdir, exist_ok=True)
    console.info(f"attach 到 {args.tool}（工作目录 {workdir}）… Ctrl-D 结束后返回")
    try:
        rc = ptty_mod.run_attached(args.tool, workdir, prompt)
    except NotImplementedError as e:
        console.error(str(e))
        return 2
    return rc


def cmd_verify(args, cfg) -> int:
    import shutil

    from .llm import check_key

    claude_ok = bool(shutil.which(cfg.claude.binary) or shutil.which(cfg.claude.binary.split()[0]))
    codex_ok = bool(shutil.which(cfg.codex.binary) or shutil.which(cfg.codex.binary.split()[0]))
    key_ok, key_note = check_key(cfg.llm)
    results = {"claude": claude_ok, "codex": codex_ok, "llm_key": key_ok, "llm_note": key_note}
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        console.ok(f"claude: 已安装") if claude_ok else console.error("claude: 未安装")
        console.ok(f"codex: 已安装") if codex_ok else console.error("codex: 未安装")
        console.ok(key_note) if key_ok else console.error(key_note)
        if not codex_ok:
            console.info("提示：codex 未安装时可用 npm install -g @openai/codex")
    return 0


def cmd_init(args, cfg) -> int:
    path = args.config or "config.json"
    if os.path.exists(path):
        console.warn(f"{path} 已存在，跳过")
    else:
        save_example_config(path)
        console.ok(f"已生成 {path}，请按需填写 api_key_env / 权限设置")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    try:
        if args.command == "run":
            return cmd_run(args, cfg)
        if args.command == "plan":
            return cmd_plan(args, cfg)
        if args.command == "attach":
            return cmd_attach(args, cfg)
        if args.command == "verify-config":
            return cmd_verify(args, cfg)
        if args.command == "init":
            return cmd_init(args, cfg)
    except KeyboardInterrupt:
        console.error("已中断")
        return 130
    except Exception as e:  # noqa: BLE001
        console.error(f"出错: {e}")
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
