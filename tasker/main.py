
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from . import console
from .config import load_config, save_example_config
from .llm import LLMError
from .models import subtask_to_dict
from .planner import _auto_match_template, plan_with_llm, plan_with_rules

def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tasker", description="交互式目标驱动多智能体编排器：输入目标 → 拆分 → Claude Agent SDK / Codex App Server 执行 → 直到 goal 达成")
    p.add_argument("--version", action="version", version=f"tasker {__version__}")
    p.add_argument("--config", default=None, help="配置文件路径（默认 ./config.json）")
    sub = p.add_subparsers(dest="command", required=False)

    rp = sub.add_parser("repl", help="进入交互式 REPL（无参数默认）")
    rp.add_argument("--template", default=None, help="REPL 使用的任务拆解模板名称或 JSON 文件路径")

    pl = sub.add_parser("plan", help="打印拆分计划")
    pl.add_argument("prompt", nargs="?", default="")
    pl.add_argument("--plan-rules", action="store_true")
    pl.add_argument("--json", action="store_true", help="以 JSON 输出计划")
    pl.add_argument("--template", default=None, help="任务拆解模板名称（从 tasker-template 模板库查找，也兼容文件路径）")

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


def cmd_repl(args, cfg) -> int:
    """进入交互式 REPL（无参数默认入口）。"""
    from .repl import main_loop

    template = _load_template(args.template) if getattr(args, "template", None) else None
    return main_loop(cfg, template=template)


def _load_template(name: str) -> dict | None:

    import os

    if os.sep in name or "/" in name or name.endswith(".json"):
        return _load_template_from_file(name)

    try:
        from .template_compiler import load_named_template

        tpl = load_named_template(name)
        if tpl:
            return tpl
        console.warn(f"模板库中未找到模板: {name}")
        return None
    except ImportError:
        console.warn("tasker-template 包未安装，无法按名称查找模板")
        return None
    except Exception as e:
        console.warn(f"模板查找失败: {e}")
        return None


def _load_template_from_file(path: str) -> dict | None:

    from pathlib import Path

    p = Path(path).expanduser().resolve()
    if not p.exists():
        console.warn(f"模板文件不存在: {p}")
        return None

    if p.suffix.lower() == ".json":
        try:
            import json

            data = json.loads(p.read_text(encoding="utf-8"))
            if "template_name" in data and "suggested_tasks" in data:
                data.pop("_meta", None)
                return data
            if "objective" in data and "tasks" in data:
                items = [
                    {
                        "title": t.get("title", ""),
                        "description": t.get("description", ""),
                        "acceptance": t.get("acceptance", ""),
                        "tool": t.get("tool") or t.get("skill", ""),
                        "internal_loop": t.get("internal_loop", t.get("loop")),
                    }
                    for t in data.get("tasks", [])
                ]
                return {
                    "template_name": data.get("objective", ""),
                    "source_file": str(p),
                    "system_prompt_extension": data.get("rationale", ""),
                    "suggested_tasks": items,
                }
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    try:
        from template import extract_template as _extract

        tpl = _extract(str(p))
        return tpl.to_dict()
    except ImportError:
        pass
    except Exception as e:
        console.warn(f"模板提取失败（{e}），回退到原始 JSON 解析")

    try:
        import json

        data = json.loads(p.read_text(encoding="utf-8"))
        if "template_name" in data:
            return data
        if "objective" in data and "tasks" in data:
            items = [
                {
                    "title": t.get("title", ""),
                    "description": t.get("description", ""),
                    "acceptance": t.get("acceptance", ""),
                    "tool": t.get("tool") or t.get("skill", ""),
                }
                for t in data.get("tasks", [])
            ]
            return {
                "template_name": data.get("objective", ""),
                "source_file": str(p),
                "system_prompt_extension": data.get("rationale", ""),
                "suggested_tasks": items,
            }
        console.warn(f"模板 JSON 格式不兼容: {p}")
        return None
    except Exception as e:
        console.warn(f"模板加载失败: {e}")
        return None


def cmd_plan(args, cfg) -> int:
    prompt = _read_prompt(args.prompt)
    template = _load_template(args.template) if args.template else _auto_match_template(prompt)
    if args.plan_rules:
        plan = plan_with_rules(prompt, template=template)
    else:
        try:
            plan = plan_with_llm(prompt, cfg, emit=lambda e: console.dim(f"[planner] {e.text}"), template=template)
        except LLMError as e:
            console.warn(f"任务拆分 LLM 不可用（{e}），回退到规则拆分")
            plan = plan_with_rules(prompt, template=template)
    if args.json:
        print(
            json.dumps(
                {
                    "objective": plan.objective,
                    "rationale": plan.rationale,
                    "tasks": [subtask_to_dict(t) for t in plan.tasks],
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


def cmd_verify(args, cfg) -> int:
    import shutil

    from .llm import check_key

    import importlib.util

    claude_ok = importlib.util.find_spec("claude_agent_sdk") is not None
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
    from pathlib import Path

    from .config import _user_config_path

    if args.config:
        path = args.config
    else:
        p = _user_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        path = str(p)
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
        if args.command is None or args.command == "repl":
            return cmd_repl(args, cfg)
        if args.command == "plan":
            return cmd_plan(args, cfg)
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
