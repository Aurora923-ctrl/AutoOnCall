"""AIOps Tool Registry for local, MCP, and adapter-backed tools."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

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
        self._trusted_tools: set[str] = set()
        self._default_incident: dict[str, Any] | None = None

    def register(self, tool: AIOpsTool, *, trusted: bool = False) -> None:
        """Register a tool, keeping untrusted extensions behind approval by default."""
        if not tool.name:
            raise ValueError("Tool name is required")
        if tool.name in self._tools:
            raise ValueError(f"Tool is already registered: {tool.name}")
        _validate_registered_contract(tool)
        self._tools[tool.name] = tool
        if trusted:
            self._trusted_tools.add(tool.name)

    def get(self, name: str) -> AIOpsTool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def replace_for_test(self, tool: AIOpsTool) -> None:
        """Replace an existing tool only for deterministic test/evaluation fixtures."""
        if not tool.name or tool.name not in self._tools:
            raise ValueError(f"Tool is not registered: {tool.name}")
        self._tools[tool.name] = tool

    def get_policy_metadata(self, name: str) -> dict[str, Any] | None:
        """Return registry-owned policy metadata instead of trusting extension declarations."""
        tool = self.get(name)
        if tool is None:
            return None
        if name not in self._trusted_tools:
            return {
                "name": name,
                "read_only": False,
                "risk_level": "high",
                "trusted": False,
            }
        return {
            "name": name,
            "read_only": tool.read_only,
            "risk_level": tool.risk_level,
            "trusted": True,
        }

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
        normalized_input_args = _apply_tool_input_defaults(input_args, tool.input_schema or {})
        step_tool_name = _step_tool_name(step)
        if step_tool_name and step_tool_name != name:
            reason = (
                f"Tool invocation mismatch: requested {name}, "
                f"but policy step declares {step_tool_name}"
            )
            return ToolExecutionResult(
                tool_name=name,
                status="failed",
                input_args=normalized_input_args,
                output={
                    "status": "failed",
                    "source": "policy_guard",
                    "policy": "forbidden",
                    "risk_level": "high",
                    "read_only": tool.read_only,
                    "reason": reason,
                    "matched_rules": ["tool:step-name-mismatch"],
                    "summary": f"工具执行被 Policy Guard 拦截: {reason}",
                },
                risk_level="high",
                read_only=tool.read_only,
                error_message=reason,
                metadata={
                    "policy_guard": {
                        "policy": "forbidden",
                        "risk_level": "high",
                        "read_only": tool.read_only,
                        "matched_rules": ["tool:step-name-mismatch"],
                    }
                },
            )
        from app.agent.aiops.risk_controller import assess_plan_step

        policy_step = _policy_step(name, normalized_input_args, tool, step)
        decision = assess_plan_step(
            policy_step,
            tool_registry=self,
            incident=incident if incident is not None else self._default_incident,
        )
        if decision.policy != "allow":
            return ToolExecutionResult(
                tool_name=name,
                status="failed",
                input_args=normalized_input_args,
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
        validation_failure = _validate_tool_input(name, normalized_input_args, tool)
        if validation_failure is not None:
            return validation_failure
        result = await tool.arun(normalized_input_args)
        output_failure = _validate_tool_output(name, result, tool)
        return output_failure or result


def create_default_tool_registry(langchain_tools: list[Any] | None = None) -> ToolRegistry:
    """Build the default registry from live adapters and MCP/LangChain tools."""
    registry = ToolRegistry()
    registry.register(QueryMetricsTool(langchain_tools), trusted=True)
    registry.register(QueryLogsTool(langchain_tools), trusted=True)
    registry.register(QueryServiceContextTool(), trusted=True)
    registry.register(QueryDeployHistoryTool(), trusted=True)
    registry.register(QueryRedisStatusTool(), trusted=True)
    registry.register(QueryK8sStatusTool(), trusted=True)
    registry.register(QueryMySQLStatusTool(), trusted=True)
    registry.register(SearchRunbookTool(), trusted=True)
    registry.register(SearchHistoryTicketTool(), trusted=True)
    registry.register(SuggestRemediationTool(), trusted=True)
    return registry


def _policy_step(
    name: str,
    input_args: dict[str, Any],
    tool: AIOpsTool,
    step: PlanStep | dict[str, Any] | None,
) -> PlanStep:
    if isinstance(step, PlanStep):
        return step.model_copy(
            update={
                "tool_name": name,
                "input_args": dict(input_args or {}),
            }
        )
    if isinstance(step, dict):
        try:
            return PlanStep(**step).model_copy(
                update={
                    "tool_name": name,
                    "input_args": dict(input_args or {}),
                }
            )
        except Exception:
            pass
    return PlanStep(
        tool_name=name,
        purpose=getattr(tool, "description", "") or f"Run tool {name}",
        input_args=dict(input_args or {}),
        expected_evidence="Tool policy guard preflight",
        risk_level=getattr(tool, "risk_level", "low"),
    )


def _step_tool_name(step: PlanStep | dict[str, Any] | None) -> str:
    if isinstance(step, PlanStep):
        return step.tool_name
    if isinstance(step, dict):
        return str(step.get("tool_name") or "")
    return ""


def _validate_tool_input(
    name: str,
    input_args: dict[str, Any],
    tool: AIOpsTool,
) -> ToolExecutionResult | None:
    schema = tool.input_schema or {}
    if not schema:
        return None
    try:
        Draft202012Validator.check_schema(schema)
        error = next(Draft202012Validator(schema).iter_errors(input_args), None)
    except SchemaError as exc:
        return _invalid_input_result(
            name,
            input_args,
            tool,
            "Tool input schema is invalid",
            path=list(exc.path),
            validator="schema",
        )
    if error is None:
        return None
    return _invalid_input_result(
        name,
        input_args,
        tool,
        _validation_error_message(error),
        path=list(error.absolute_path),
        validator=str(error.validator or "unknown"),
    )


def _validate_tool_output(
    name: str,
    result: ToolExecutionResult,
    tool: AIOpsTool,
) -> ToolExecutionResult | None:
    schema = tool.output_schema or {}
    if not schema or result.status != "success":
        return None
    try:
        Draft202012Validator.check_schema(schema)
        error = next(Draft202012Validator(schema).iter_errors(result.output), None)
    except SchemaError as exc:
        return _invalid_output_result(
            name,
            result,
            tool,
            "Tool output schema is invalid",
            path=list(exc.path),
            validator="schema",
        )
    if error is None:
        return None
    return _invalid_output_result(
        name,
        result,
        tool,
        _validation_error_message(error, subject="output"),
        path=list(error.absolute_path),
        validator=str(error.validator or "unknown"),
    )


def _apply_tool_input_defaults(
    input_args: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(input_args)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return normalized
    for key, property_schema in properties.items():
        if key in normalized or not isinstance(property_schema, dict):
            continue
        if "default" in property_schema:
            normalized[key] = deepcopy(property_schema["default"])
    return normalized


def _invalid_input_result(
    name: str,
    input_args: dict[str, Any],
    tool: AIOpsTool,
    error_message: str,
    *,
    path: list[Any],
    validator: str,
) -> ToolExecutionResult:
    validation_error = {"path": path, "validator": validator}
    return ToolExecutionResult(
        tool_name=name,
        status="failed",
        input_args=input_args,
        output={
            "status": "failed",
            "source": "tool_contract",
            "error_type": "invalid_input",
            "error_message": error_message,
            "validation_error": validation_error,
        },
        risk_level=tool.risk_level,
        read_only=tool.read_only,
        error_message=error_message,
        metadata={"input_validation": validation_error},
    )


def _invalid_output_result(
    name: str,
    result: ToolExecutionResult,
    tool: AIOpsTool,
    error_message: str,
    *,
    path: list[Any],
    validator: str,
) -> ToolExecutionResult:
    validation_error = {"path": path, "validator": validator}
    metadata = dict(result.metadata or {})
    metadata["output_validation"] = validation_error
    return ToolExecutionResult(
        tool_name=name,
        status="failed",
        input_args=result.input_args,
        output={
            "status": "failed",
            "source": "tool_contract",
            "error_type": "invalid_output",
            "error_message": error_message,
            "validation_error": validation_error,
        },
        latency_ms=result.latency_ms,
        risk_level=tool.risk_level,
        read_only=tool.read_only,
        error_message=error_message,
        metadata=metadata,
    )


def _validation_error_message(error: ValidationError, *, subject: str = "input") -> str:
    path = ".".join(str(item) for item in error.absolute_path)
    location = f" at {path}" if path else ""
    return f"Invalid tool {subject}{location}: {error.message}"


def _validate_registered_contract(tool: AIOpsTool) -> None:
    """Reject invalid contracts at registration instead of waiting for first execution."""
    for subject, schema in (
        ("input", tool.input_schema or {}),
        ("output", tool.output_schema or {}),
    ):
        if not schema:
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ValueError(f"Tool {tool.name} has an invalid {subject} schema") from exc
