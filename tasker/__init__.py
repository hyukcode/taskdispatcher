"""tasker —— 多智能体任务编排器。

通过 LLM API 把用户 prompt 拆分为子任务，分派给 Claude Code 与 Codex 执行，
并完整采集两者的"思维链 / 工具调用 / 交互 / 审批请求"事件，写入会话事件日志。
"""

from __future__ import annotations

from pathlib import Path

# 安装后优先读打包时注入的 _version.py；开发环境（源码）回退到读 pyproject.toml
__version__ = "0.0.0"
try:
    from ._version import __version__  # type: ignore[attr-defined]  # noqa: F401
except ImportError:
    try:
        _pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        _text = _pyproject.read_text(encoding="utf-8")
        for _line in _text.splitlines():
            _left, _separator, _right = _line.partition("=")
            if not _separator or _left.strip() != "version":
                continue
            _value = _right.strip()
            if _value.startswith('"'):
                _end = _value.find('"', 1)
                if _end > 1:
                    __version__ = _value[1:_end]
                    break
    except (OSError, UnicodeError, ImportError):
        pass
