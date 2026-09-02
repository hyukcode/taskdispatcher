
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
        base = (cfg.base_url or "https://api.anthropic.com/v1").rstrip("/")
        url = base if base.endswith("/messages") else base + "/messages"
    else:
        base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=_build_headers(cfg),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:  # noqa: S310
            try:
                return json.loads(resp.read().decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise LLMError("LLM 返回内容不是合法 JSON") from exc
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except (OSError, UnicodeDecodeError) as exc:
            # HTTP 错误正文读取失败不应掩盖原始状态码。
            detail = f"（错误详情读取失败: {exc}）"
        raise LLMError(f"LLM HTTP {e.code}: {e.reason} {detail[:800]}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"LLM 网络错误: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise LLMError(f"LLM 网络错误: {e}") from e


def chat(
    cfg: LLMConfig,
    messages: list[dict[str, Any]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    body: dict[str, Any] = {
        "model": cfg.model,
        "temperature": cfg.temperature if temperature is None else temperature,
        "max_tokens": cfg.max_tokens if max_tokens is None else max_tokens,
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
