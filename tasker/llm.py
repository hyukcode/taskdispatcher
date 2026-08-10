"""零依赖 LLM 客户端（stdlib urllib）。

- provider=anthropic → Anthropic Messages API
- provider=openai     → OpenAI 兼容接口（base_url 可指向任意网关/DeepSeek/Ollama）

配合 config.api_key_env 从环境变量取 key。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .config import LLMConfig


class LLMError(RuntimeError):
    pass


def _build_headers(cfg: LLMConfig) -> dict[str, str]:
    key = os.environ.get(cfg.api_key_env, "")
    if cfg.provider == "anthropic":
        return {
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "anthropic-dangerous-direct-browser-access": "true",
        }
    return {"content-type": "application/json", "authorization": f"Bearer {key}"}


def _post(cfg: LLMConfig, body: dict[str, Any]) -> dict[str, Any]:
    if cfg.provider == "anthropic":
        url = (cfg.base_url or "https://api.anthropic.com/v1/messages").rstrip("/")
    else:
        url = (cfg.base_url or "https://api.openai.com/v1/chat/completions").rstrip("/")
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=_build_headers(cfg),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise LLMError(f"LLM HTTP {e.code}: {e.reason} {detail[:800]}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"LLM 网络错误: {e.reason}") from e


def chat(
    cfg: LLMConfig,
    messages: list[dict[str, Any]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """发一轮对话，返回纯文本。"""
    body: dict[str, Any] = {
        "model": cfg.model,
        "temperature": cfg.temperature if temperature is None else temperature,
        "max_tokens": max_tokens or cfg.max_tokens,
        "messages": messages,
    }
    data = _post(cfg, body)
    if cfg.provider == "anthropic":
        text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
    else:
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"LLM 返回结构异常: {json.dumps(data)[:500]}") from e
    if not text:
        raise LLMError(f"LLM 返回空内容: {json.dumps(data)[:500]}")
    return text


def check_key(cfg: LLMConfig) -> tuple[bool, str]:
    key = os.environ.get(cfg.api_key_env, "")
    if not key:
        return False, f"未设置环境变量 {cfg.api_key_env}（用于任务拆分 LLM）"
    if key.startswith("sk-") is False and len(key) < 10:
        return False, f"环境变量 {cfg.api_key_env} 看起来不像有效的 API Key"
    return True, f"环境变量 {cfg.api_key_env} 已设置"
