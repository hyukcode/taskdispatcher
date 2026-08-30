"""Code-agent runner 的 Strategy Registry 与 Factory。

GraphExecutor 只依赖统一的 RunnerBase 接口；协议实现通过注册表选择。
这同时保留了清晰的扩展点：新增执行器只需实现 RunnerBase 并加入注册表。
"""

from __future__ import annotations

from typing import Type

from .codex_app_server_runner import CodexAppServerRunner
from .config import Config
from .models import TaskRun
from .runner_base import EventSink, RunnerBase
from .sdk_runner import SdkClaudeRunner


RunnerType = Type[RunnerBase]

RUNNER_REGISTRY: dict[str, RunnerType] = {
    "claude": SdkClaudeRunner,
    "codex": CodexAppServerRunner,
}


def runner_type(executor: str) -> RunnerType:
    """返回执行器策略；未知 executor 统一在工厂边界报错。"""
    try:
        return RUNNER_REGISTRY[executor]
    except KeyError as exc:
        supported = ", ".join(sorted(RUNNER_REGISTRY))
        raise ValueError(f"未知 executor: {executor}（支持：{supported}）") from exc


def create_runner(
    executor: str,
    cfg: Config,
    run: TaskRun,
    workdir: str,
    on_event: EventSink,
    prompt: str,
    *,
    broker=None,
) -> RunnerBase:
    """按 executor 创建统一 runner；调用方不需要知道具体协议类。"""
    return runner_type(executor)(cfg, run, workdir, on_event, prompt, broker=broker)
