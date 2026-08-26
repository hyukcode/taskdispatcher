from __future__ import annotations

import time
from pathlib import Path

from .models import Plan, TaskRun


def write_report(plan: Plan, runs: list[TaskRun], prompt: str, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = out / f"run-{ts}.md"

    lines: list[str] = []
    lines.append(f"# 多智能体运行报告  {ts}\n")
    lines.append(f"## 目标\n\n> {prompt}\n")
    lines.append("## 计划\n")
    for t in plan.tasks:
        lines.append(f"- **{t.id}** `[{t.executor}]` ← {','.join(t.depends_on) or '—'}  {t.title}")
        lines.append(f"  - {t.description.replace(chr(10), chr(10)+'    ')}")
    lines.append("")

    for run in runs:
        t = run.task
        lines.append(f"\n## 任务 {t.id} — {t.title}  `[{t.executor}]`")
        lines.append(f"- 状态: {run.status}  用时: {run.duration:.1f}s  成本: ${run.cost_usd:.4f}")
        if run.error:
            lines.append(f"- 错误: {run.error}")
        if run.raw_log_path:
            lines.append(f"- 原始事件日志: `{run.raw_log_path}`")
        lines.append("")
        lines.append("### 思维链")
        for e in run.thinking_events:
            lines.append(f"```\n{e.text}\n```")
        lines.append("")
        lines.append("### 工具调用")
        for e in run.tool_events:
            mark = "→" if e.kind == "tool_use" else "←"
            if e.kind == "tool_use":
                lines.append(f"**{mark} {e.data.get('tool','?')}**\n")
                lines.append(f"```json\n{e.data.get('input')}\n```")
            else:
                lines.append(f"*{mark} 结果*: {e.text[:2000]}")
        lines.append("")
        lines.append("### 交互 / 审批")
        for e in run.events:
            if e.kind in ("permission_request", "permission_result", "user_message", "interaction"):
                lines.append(f"- `{e.kind}` {e.text}")
        lines.append("")
        lines.append("### 最终输出")
        lines.append(f"```\n{run.output}\n```")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
