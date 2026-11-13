"""Stdio MCP server exposing the showcase tool registry over the fictional firm data: python -m pierre.mcp_server."""
from __future__ import annotations

import inspect
from typing import Any, Callable, List, Optional

from mcp.server.fastmcp import FastMCP

from pierre.agent.backend import InMemoryFirmStore
from pierre.agent.registry import REGISTRY, ToolContext, ToolSpec, bind_tools
from pierre.agent.tools import ToolDispatcher

_ANNOTATIONS = {"string": str, "integer": int, "number": float, "array": List[str]}


def _tool_function(spec: ToolSpec, dispatcher: ToolDispatcher) -> Callable[..., Any]:
    required = set(spec.input_schema["required"])
    params = [
        inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if name in required else None,
            annotation=_ANNOTATIONS[prop["type"]] if name in required else Optional[_ANNOTATIONS[prop["type"]]],
        )
        for name, prop in spec.input_schema["properties"].items()
    ]

    def call(**kwargs: Any) -> Any:
        return dispatcher.execute(spec.name, {k: v for k, v in kwargs.items() if v is not None})

    call.__name__ = spec.name
    call.__doc__ = spec.description
    call.__signature__ = inspect.Signature(params)
    return call


def build_server(store: Optional[InMemoryFirmStore] = None) -> FastMCP:
    ctx = ToolContext(store or InMemoryFirmStore())
    dispatcher = ToolDispatcher(bind_tools(ctx))
    server = FastMCP("pierre-demo")
    for spec in REGISTRY:
        server.add_tool(_tool_function(spec, dispatcher), name=spec.name, description=spec.description)
    return server


if __name__ == "__main__":
    build_server().run(transport="stdio")
