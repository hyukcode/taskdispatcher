"""跨模块复用的文本格式化工具。"""

from __future__ import annotations

import json
import logging


logger = logging.getLogger(__name__)


def compact_json(value, width: int) -> str:
    """将对象压缩为单行 JSON，并限制展示宽度。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        logger.debug("对象无法序列化为 JSON，回退 str(): %s", exc, exc_info=True)
        text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def extract_json_object(text: str) -> dict:
    """从模型文本中提取第一个合法 JSON 对象。

    支持普通文本、Markdown fenced code block，以及 JSON 后的解释文字。
    使用 raw_decode 而不是简单的首尾大括号截取，避免字符串中的大括号破坏解析。
    """
    source = (text or "").strip()
    if not source:
        raise ValueError("文本为空，未找到 JSON 对象")

    candidates = [source]
    if "```" in source:
        candidates.extend(part.strip() for part in source.split("```") if part.strip())

    decoder = json.JSONDecoder()
    for candidate in candidates:
        start = candidate.find("{")
        while start >= 0:
            try:
                value, _ = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                start = candidate.find("{", start + 1)
                continue
            if isinstance(value, dict):
                return value
            start = candidate.find("{", start + 1)
    raise ValueError("文本中没有合法 JSON 对象")
