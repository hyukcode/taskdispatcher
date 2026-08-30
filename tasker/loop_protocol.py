from __future__ import annotations

import json


def parse_loop_decision(output: str) -> dict | None:
    """解析任务内部 loop 的统一结果格式。"""
    text = (output or "").strip()
    if not text:
        return None
    candidates = [text]
    if "```" in text:
        candidates.extend(part.strip() for part in text.split("```") if part.strip())
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            start = candidate.find("{")
            if start < 0:
                continue
            try:
                value, _ = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict) and value.get("status") in {"passed", "needs_iteration"}:
            return value
    return None
