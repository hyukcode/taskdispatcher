"""tasker —— 多智能体任务编排器。

通过 LLM API 把用户 prompt 拆分为子任务，分派给 Claude Code 与 Codex 执行，
并完整采集两者的"思维链 / 工具调用 / 交互 / 审批请求"事件，输出 Markdown 报告。
"""

from __future__ import annotations

import re
from pathlib import Path

# 安装后优先读打包时注入的 _version.py；开发环境（源码）回退到读 pyproject.toml
__version__ = "0.0.0"
try:
    from ._version import __version__  # type: ignore[attr-defined]  # noqa: F401
except ImportError:
    try:
        _pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        _text = _pyproject.read_text(encoding="utf-8")
        _m = re.search(r'^version\s*=\s*"([^"]+)"', _text, re.MULTILINE)
        if _m:
            __version__ = _m.group(1)
    except Exception:
        pass
