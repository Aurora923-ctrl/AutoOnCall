"""AIOps Tool Registry for local, MCP, and adapter-backed tools."""

from __future__ import annotations

from typing import Any

from app.models.plan import PlanStep
from app.tools.base import AIOpsTool, ToolContract, ToolExecutionResult
from app.tools.context_tool import QueryDeployHistoryTool, QueryServiceContextTool
from app.tools.logs_tool import QueryLogsTool
from app.tools.metrics_tool import QueryMetricsTool
from app.tools.ops_tool import (
    QueryK8sStatusTool,
    QueryMySQLStatusTool,
    SearchHistoryTicketTool,
    SuggestRemediationTool,
)
from app.tools.redis_tool import QueryRedisStatusTool
from app.tools.runbook_tool import SearchRunbookTool


class ToolRegistry:
    """Registry that exposes stable AIOps tool names to the Executor."""

    def __init__(self) -> None:
        self._tools: dict[str, AIOpsTool] = {}
        self._default_incident: dict[str, Any] | None = None

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

    def with_incident_context(self, incident: dict[str, Any] | None) -> ToolRegistry:
        """Attach incident context used by the execution-time policy guard."""
        self._default_incident = dict(incident or {})
        return self

    async def arun(
        self,
        name: str,
        input_args: dict[str, Any],
        *,
        incident: dict[str, Any] | None = None,
        step: PlanStep | dict[str, Any] | None = None,
    ) -> ToolExecutionResult:
        """Run a registered tool by name."""
        tool = self.get(name)
        if not tool:
            return ToolExecutionResult(
                tool_name=name,
                status="failed",
                input_args=input_args,
                error_message=f"Tool is not registered: {name}",
            )
        from app.agent.aiops.risk_controller import assess_plan_step

        policy_step = _policy_step(name, input_args, tool, step)
        decision = assess_plan_step(
            policy_step,
            tool_registry=self,
            incident=incident if incident is not None else self._default_incident,
        )
        if decision.policy != "allow":
            return ToolExecutionResult(
                tool_name=name,
                status="failed",
                input_args=input_args,
                output={
                    "status": "failed",
                    "source": "policy_guard",
                    "policy": decision.policy,
                    "risk_level": decision.risk_level,
                    "read_only": decision.read_only,
                    "reason": decision.reason,
                    "matched_rules": decision.matched_rules,
                    "summary": f"工具执行被 Policy Guard 拦截: {decision.reason}",
                },
                risk_level=decision.risk_level,
                read_only=decision.read_only,
                error_message=decision.reason,
                metadata={
                    "policy_guard": {
                        "policy": decision.policy,
                        "risk_level": decision.risk_level,
                        "read_only": decision.read_only,
                        "matched_rules": decision.matched_rules,
                    }
                },
            )
        return await tool.arun(input_args)


def create_default_tool_registry(langchain_tools: list[Any] | None = None) -> ToolRegistry:
    """Build the default registry from live adapters and MCP/LangChain tools."""
    registry = ToolRegistry()
    registry.register(QueryMetricsTool(langchain_tools))
    registry.register(QueryLogsTool(langchain_tools))
    registry.register(QueryServiceContextTool())
    registry.register(QueryDeployHistoryTool())
    registry.register(QueryRedisStatusTool())
    registry.register(QueryK8sStatusTool())
    registry.register(QueryMySQLStatusTool())
    registry.register(SearchRunbookTool())
    registry.register(SearchHistoryTicketTool())
    registry.register(SuggestRemediationTool())
    return registry


def _policy_step(
    name: str,
    input_args: dict[str, Any],
    tool: AIOpsTool,
    step: PlanStep | dict[str, Any] | None,
) -> PlanStep:
    if isinstance(step, PlanStep):
        return step
    if isinstance(step, dict):
        try:
            return PlanStep(**step)
        except Exception:
            pass
    return PlanStep(
        tool_name=name,
        purpose=getattr(tool, "description", "") or f"Run tool {name}",
        input_args=dict(input_args or {}),
        expected_evidence="Tool policy guard preflight",
        risk_level=getattr(tool, "risk_level", "low"),
    )
