"""跨模块复用的文本格式化工具。"""

from __future__ import annotations

import json


def compact_json(value, width: int) -> str:
    """将对象压缩为单行 JSON，并限制展示宽度。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"
