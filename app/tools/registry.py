"""AIOps Tool Registry for local, MCP, and mock tools."""

from __future__ import annotations

from typing import Any

from app.tools.alert_tool import QueryAlertsTool
from app.tools.base import AIOpsTool, ToolContract, ToolExecutionResult
from app.tools.context_tool import QueryDeployHistoryTool, QueryServiceContextTool
from app.tools.logs_tool import QueryLogsTool
from app.tools.message_queue_tool import QueryMessageQueueStatusTool
from app.tools.metrics_tool import QueryMetricsTool
from app.tools.mock_ops_tool import (
    QueryK8sStatusTool,
    QueryMySQLStatusTool,
    SearchHistoryTicketTool,
    SuggestRemediationTool,
)
from app.tools.redis_tool import QueryRedisStatusTool
from app.tools.runbook_tool import SearchRunbookTool
from app.tools.tracing_tool import QueryTracesTool


class ToolRegistry:
    """Registry that exposes stable AIOps tool names to the Executor."""

    def __init__(self):
        self._tools: dict[str, AIOpsTool] = {}

    def register(self, tool: AIOpsTool) -> None:
        """Register a tool by its stable name."""
        if not tool.name:
            raise ValueError("Tool name is required")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AIOpsTool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[dict[str, Any]]:
        """Return metadata for all registered tools."""
        return [contract.model_dump(mode="json") for contract in self.list_contracts()]

    def list_contracts(self) -> list[ToolContract]:
        """Return auditable contracts for all registered tools."""
        return [tool.contract() for tool in self._tools.values()]

    async def arun(self, name: str, input_args: dict[str, Any]) -> ToolExecutionResult:
        """Run a registered tool by name."""
        tool = self.get(name)
        if not tool:
            return ToolExecutionResult(
                tool_name=name,
                status="failed",
                input_args=input_args,
                error_message=f"Tool is not registered: {name}",
            )
        return await tool.arun(input_args)


def create_default_tool_registry(langchain_tools: list[Any] | None = None) -> ToolRegistry:
    """Build the default registry from MCP/LangChain tools and local mock tools."""
    registry = ToolRegistry()
    registry.register(QueryAlertsTool())
    registry.register(QueryMetricsTool(langchain_tools))
    registry.register(QueryLogsTool(langchain_tools))
    registry.register(QueryTracesTool())
    registry.register(QueryServiceContextTool())
    registry.register(QueryDeployHistoryTool())
    registry.register(QueryMessageQueueStatusTool())
    registry.register(QueryRedisStatusTool())
    registry.register(QueryK8sStatusTool())
    registry.register(QueryMySQLStatusTool())
    registry.register(SearchRunbookTool())
    registry.register(SearchHistoryTicketTool())
    registry.register(SuggestRemediationTool())
    return registry
