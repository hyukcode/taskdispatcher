from __future__ import annotations

from .formatting import extract_json_object


def parse_loop_decision(output: str) -> dict | None:
    """解析任务内部 loop 的统一结果格式。"""
    text = (output or "").strip()
    if not text:
        return None
    try:
        value = extract_json_object(text)
    except ValueError:
        return None
    if value.get("status") in {"passed", "needs_iteration"}:
        return value
    return None
