from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from .domain.tool import (
    ToolSpec,
)

from .domain.tool_call import (
    ToolInvocation,
)

# adapter负责“怎么调用”

class ToolAdapterError(
    RuntimeError
):
    pass


class ToolTransientError(
    ToolAdapterError
):
    """
    明确知道这次调用失败，
    但错误是暂时性的。
    """


class ToolOutcomeUnknown(
    ToolAdapterError
):
    """
    无法判断副作用是否已经发生。
    """


class ToolAdapter(
    ABC
):

    @abstractmethod
    def execute(
        self,
        spec: ToolSpec,
        invocation: ToolInvocation,
    ) -> Any:
        ...


LocalHandler = Callable[
    [
        dict,
        ToolInvocation,
    ],
    Any,
]


class LocalToolAdapter(
    ToolAdapter
):
    def __init__(self) -> None:
        self._handlers: dict[
            str,
            LocalHandler,
        ] = {}

    def register(
        self,
        name: str,
        handler: LocalHandler,
    ) -> None:

        normalized = str(name).strip().casefold()

        if not normalized:
            raise ValueError(
                "handler name empty"
            )

        if normalized in (
            self._handlers
        ):
            raise ValueError(
                f"handler already exists: "
                f"{name}"
            )

        self._handlers[
            normalized
        ] = handler

    def execute(
        self,
        spec: ToolSpec,
        invocation: ToolInvocation,
    ) -> Any:

        handler = self._handlers.get(spec.name.casefold())

        if handler is None:
            raise ToolAdapterError(
                "no local handler for "
                f"{spec.name}"
            )

        return handler(
            dict(
                invocation.input_data
            ),
            invocation,
        )


McpCaller = Callable[
    [
        ToolSpec,
        dict,
        ToolInvocation,
    ],
    Any,
]


class McpToolAdapter(
    ToolAdapter
):
    def __init__(
        self,
        caller: McpCaller,
    ) -> None:
        self.caller = caller

    def execute(
        self,
        spec: ToolSpec,
        invocation: ToolInvocation,
    ) -> Any:
        return self.caller(
            spec,
            dict(
                invocation.input_data
            ),
            invocation,
        )