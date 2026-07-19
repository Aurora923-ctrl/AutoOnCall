"""Tool package exports with lazy loading for heavy registry dependencies."""

from __future__ import annotations

from typing import Any

from app.tools.base import AIOpsTool, ToolContract, ToolExecutionResult, ToolRetryPolicy
from app.tools.lazy_knowledge_tool import retrieve_knowledge
from app.tools.time_tool import get_current_time

__all__ = [
    "AIOpsTool",
    "ToolContract",
    "ToolExecutionResult",
    "ToolRetryPolicy",
    "ToolRegistry",
    "create_default_tool_registry",
    "retrieve_knowledge",
    "get_current_time",
]


def __getattr__(name: str) -> Any:
    if name in {"ToolRegistry", "create_default_tool_registry"}:
        from app.tools.registry import ToolRegistry, create_default_tool_registry

        return {
            "ToolRegistry": ToolRegistry,
            "create_default_tool_registry": create_default_tool_registry,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
