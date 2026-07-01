"""Base abstractions for industrial AIOps tools."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

ToolStatus = Literal["success", "failed"]
RiskLevel = Literal["low", "medium", "high"]


class ToolRetryPolicy(BaseModel):
    """Retry policy advertised by a tool contract."""

    max_attempts: int = Field(default=1, ge=1)
    backoff_seconds: float = Field(default=0.0, ge=0.0)
    retry_on: list[str] = Field(default_factory=list)


class ToolContract(BaseModel):
    """Auditable contract for an AIOps tool exposed to planners and reviewers."""

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = "low"
    read_only: bool = True
    timeout_seconds: float = Field(default=10.0, gt=0)
    retry_policy: ToolRetryPolicy = Field(default_factory=ToolRetryPolicy)
    data_sources: list[str] = Field(default_factory=list)
    degradation_strategy: str = "返回结构化失败结果，并保留错误信息供报告解释"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    """Structured output returned by every AIOps tool."""

    tool_name: str
    status: ToolStatus
    input_args: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    latency_ms: float = 0.0
    risk_level: RiskLevel = "low"
    read_only: bool = True
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AIOpsTool:
    """Uniform interface for local, MCP, and mock AIOps tools."""

    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {}
    output_schema: dict[str, Any] = {}
    risk_level: RiskLevel = "low"
    read_only: bool = True
    timeout_seconds: float = 10.0
    retry_policy: ToolRetryPolicy | dict[str, Any] = ToolRetryPolicy()
    data_sources: list[str] = []
    degradation_strategy: str = "返回结构化失败结果，并保留错误信息供报告解释"

    async def arun(self, input_args: dict[str, Any]) -> ToolExecutionResult:
        """Run the tool with timeout and structured error handling."""
        started_at = time.perf_counter()
        safe_args = dict(input_args or {})

        try:
            output = await asyncio.wait_for(self._call(safe_args), timeout=self.timeout_seconds)
            status: ToolStatus = "failed" if is_failed_tool_output(output) else "success"
            return ToolExecutionResult(
                tool_name=self.name,
                status=status,
                input_args=safe_args,
                output=output,
                latency_ms=elapsed_ms(started_at),
                risk_level=self.risk_level,
                read_only=self.read_only,
                error_message=extract_tool_error_message(output) if status == "failed" else None,
            )
        except Exception as exc:
            return ToolExecutionResult(
                tool_name=self.name,
                status="failed",
                input_args=safe_args,
                latency_ms=elapsed_ms(started_at),
                risk_level=self.risk_level,
                read_only=self.read_only,
                error_message=str(exc),
            )

    async def run(self, input_args: dict[str, Any]) -> ToolExecutionResult:
        """Alias kept for callers that expect a run method."""
        return await self.arun(input_args)

    async def _call(self, input_args: dict[str, Any]) -> Any:
        """Subclasses implement actual work here."""
        raise NotImplementedError

    def contract(self) -> ToolContract:
        """Return the stable contract used by registry, UI, and safety checks."""
        retry_policy = (
            self.retry_policy
            if isinstance(self.retry_policy, ToolRetryPolicy)
            else ToolRetryPolicy(**self.retry_policy)
        )
        return ToolContract(
            name=self.name,
            description=self.description,
            input_schema=dict(self.input_schema or {}),
            output_schema=dict(self.output_schema or {}),
            risk_level=self.risk_level,
            read_only=self.read_only,
            timeout_seconds=self.timeout_seconds,
            retry_policy=retry_policy,
            data_sources=list(self.data_sources or []),
            degradation_strategy=self.degradation_strategy,
        )


async def invoke_langchain_tool(tool: Any, input_args: dict[str, Any]) -> Any:
    """Invoke a LangChain/MCP tool object with schema-aware argument filtering."""
    filtered_args = filter_tool_args(tool, input_args)
    if hasattr(tool, "ainvoke"):
        return await tool.ainvoke(filtered_args)
    if hasattr(tool, "invoke"):
        value = tool.invoke(filtered_args)
        if inspect.isawaitable(value):
            return await value
        return value
    if callable(tool):
        value = tool(**filtered_args)
        if inspect.isawaitable(value):
            return await value
        return value
    raise TypeError(f"Tool {getattr(tool, 'name', tool)} is not invokable")


def filter_tool_args(tool: Any, input_args: dict[str, Any]) -> dict[str, Any]:
    """Keep only arguments accepted by a LangChain tool when schema is available."""
    schema = getattr(tool, "args_schema", None)
    fields = getattr(schema, "model_fields", None) or getattr(schema, "__fields__", None)
    if not fields:
        return dict(input_args)
    return {key: value for key, value in input_args.items() if key in fields}


def tool_map(tools: list[Any] | None) -> dict[str, Any]:
    """Index heterogeneous tool objects by their public name."""
    return {getattr(tool, "name", ""): tool for tool in tools or [] if getattr(tool, "name", "")}


def elapsed_ms(started_at: float) -> float:
    """Return elapsed milliseconds from a perf_counter start."""
    return round((time.perf_counter() - started_at) * 1000, 2)


def is_failed_tool_output(output: Any) -> bool:
    """Return True when a tool output is a structured failure payload."""
    if not isinstance(output, dict):
        return False
    status = str(output.get("status") or "").lower()
    if status in {"failed", "failure", "error"}:
        return True
    return bool(output.get("is_error") or output.get("isError"))


def extract_tool_error_message(output: Any) -> str | None:
    """Extract a useful error message from a structured failure payload."""
    if not isinstance(output, dict):
        return None
    for key in ("error_message", "error", "detail", "message", "summary"):
        value = output.get(key)
        if value:
            return str(value)
    return None
