"""Base abstractions for industrial AIOps tools."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.integrations.base import classify_adapter_error
from app.utils.public_errors import public_exception_message

ToolStatus = Literal["success", "failed"]
RiskLevel = Literal["low", "medium", "high"]
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


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

    model_config = ConfigDict(validate_assignment=True)

    tool_name: str
    status: ToolStatus
    input_args: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    latency_ms: float = 0.0
    risk_level: RiskLevel = "low"
    read_only: bool = True
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keep_status_and_error_semantics_consistent(self) -> ToolExecutionResult:
        """Normalize contradictory result envelopes before they reach evidence conversion."""
        if self.status == "success":
            output_error = extract_tool_error_message(self.output)
            if self.error_message or is_failed_tool_output(self.output):
                object.__setattr__(self, "status", "failed")
                if not self.error_message:
                    object.__setattr__(
                        self,
                        "error_message",
                        output_error or "Tool returned a structured failure result",
                    )
        return self


class AIOpsTool:
    """Uniform interface for local, MCP, and external-adapter AIOps tools."""

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

    def __init__(self) -> None:
        """Copy class-level contract defaults onto each tool instance."""
        self.input_schema = deepcopy(type(self).input_schema or {})
        self.output_schema = deepcopy(type(self).output_schema or {})
        self.data_sources = list(deepcopy(type(self).data_sources or []))
        retry_policy = type(self).retry_policy
        self.retry_policy = (
            retry_policy.model_copy(deep=True)
            if isinstance(retry_policy, ToolRetryPolicy)
            else ToolRetryPolicy(**dict(retry_policy or {}))
        )

    async def arun(self, input_args: dict[str, Any]) -> ToolExecutionResult:
        """Run the tool within one total timeout budget and an explicit retry policy."""
        started_at = time.perf_counter()
        safe_args = dict(input_args)
        retry_policy = _normalized_retry_policy(self.retry_policy)
        max_attempts = retry_policy.max_attempts if self.read_only else 1
        retry_on = {item.strip().lower() for item in retry_policy.retry_on if item.strip()}
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        attempts: list[dict[str, Any]] = []
        final_output: Any = None
        final_error: str | None = None
        stop_reason = "completed"

        for attempt in range(1, max_attempts + 1):
            attempt_started = time.perf_counter()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                final_error = public_exception_message(TimeoutError())
                attempts.append(
                    {
                        "attempt": attempt,
                        "status": "failed",
                        "failure_kind": "timeout",
                        "latency_ms": elapsed_ms(attempt_started),
                    }
                )
                stop_reason = "total_timeout_exhausted"
                break

            try:
                output = await asyncio.wait_for(self._call(safe_args), timeout=remaining)
                failed = is_failed_tool_output(output)
                failure_kind = failure_kind_from_output(output) if failed else ""
                attempts.append(
                    {
                        "attempt": attempt,
                        "status": "failed" if failed else "success",
                        "failure_kind": failure_kind,
                        "latency_ms": elapsed_ms(attempt_started),
                    }
                )
                if not failed:
                    return self._execution_result(
                        status="success",
                        input_args=safe_args,
                        output=output,
                        started_at=started_at,
                        attempts=attempts,
                        max_attempts=max_attempts,
                        stop_reason="success",
                    )

                final_output = output
                final_error = extract_tool_error_message(output)
                if not _should_retry(
                    read_only=self.read_only,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    failure_kind=failure_kind,
                    retry_on=retry_on,
                    output=output,
                ):
                    stop_reason = _retry_stop_reason(
                        attempt=attempt,
                        max_attempts=max_attempts,
                        failure_kind=failure_kind,
                        retry_on=retry_on,
                    )
                    break
            except asyncio.CancelledError:
                raise
            except TimeoutError as exc:
                failure_kind = "timeout"
                final_error = public_exception_message(exc)
                attempts.append(
                    {
                        "attempt": attempt,
                        "status": "failed",
                        "failure_kind": failure_kind,
                        "latency_ms": elapsed_ms(attempt_started),
                    }
                )
                stop_reason = "total_timeout_exhausted"
                break
            except Exception as exc:
                failure_kind = classify_adapter_error(exc)
                final_error = public_exception_message(exc)
                attempts.append(
                    {
                        "attempt": attempt,
                        "status": "failed",
                        "failure_kind": failure_kind,
                        "latency_ms": elapsed_ms(attempt_started),
                    }
                )
                if not _should_retry(
                    read_only=self.read_only,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    failure_kind=failure_kind,
                    retry_on=retry_on,
                ):
                    stop_reason = _retry_stop_reason(
                        attempt=attempt,
                        max_attempts=max_attempts,
                        failure_kind=failure_kind,
                        retry_on=retry_on,
                    )
                    break

            if not await _sleep_within_budget(retry_policy.backoff_seconds, deadline):
                final_error = public_exception_message(TimeoutError())
                stop_reason = "total_timeout_exhausted"
                break

        return self._execution_result(
            status="failed",
            input_args=safe_args,
            output=final_output,
            started_at=started_at,
            attempts=attempts,
            max_attempts=max_attempts,
            stop_reason=stop_reason,
            error_message=final_error,
        )

    def _execution_result(
        self,
        *,
        status: ToolStatus,
        input_args: dict[str, Any],
        output: Any,
        started_at: float,
        attempts: list[dict[str, Any]],
        max_attempts: int,
        stop_reason: str,
        error_message: str | None = None,
    ) -> ToolExecutionResult:
        attempt_count = len(attempts)
        retry_metadata = {
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "retried": attempt_count > 1,
            "retry_exhausted": status == "failed" and attempt_count >= max_attempts > 1,
            "stop_reason": stop_reason,
            "attempts": attempts,
            "total_timeout_seconds": self.timeout_seconds,
        }
        return ToolExecutionResult(
            tool_name=self.name,
            status=status,
            input_args=input_args,
            output=output,
            latency_ms=elapsed_ms(started_at),
            risk_level=self.risk_level,
            read_only=self.read_only,
            error_message=error_message,
            metadata={"retry": retry_metadata},
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
        return normalize_langchain_tool_output(await tool.ainvoke(filtered_args))
    if hasattr(tool, "invoke"):
        value = tool.invoke(filtered_args)
        if inspect.isawaitable(value):
            value = await value
        return normalize_langchain_tool_output(value)
    if callable(tool):
        value = tool(**filtered_args)
        if inspect.isawaitable(value):
            value = await value
        return normalize_langchain_tool_output(value)
    raise TypeError(f"Tool {getattr(tool, 'name', tool)} is not invokable")


def normalize_langchain_tool_output(value: Any) -> Any:
    """Recover structured payloads from LangChain/MCP content-and-artifact results."""
    if bool(getattr(value, "isError", False)):
        return {
            "status": "failed",
            "error_type": "mcp_error",
            "error_message": _normalized_tool_error_message(getattr(value, "content", None)),
        }

    object_structured = getattr(value, "structuredContent", None) or getattr(
        value, "structured_content", None
    )
    if isinstance(object_structured, dict):
        return dict(object_structured)

    artifact_payload = _structured_content_from_artifact(getattr(value, "artifact", None))
    if artifact_payload is not None:
        return artifact_payload

    if isinstance(value, tuple) and len(value) == 2:
        content, artifact = value
        artifact_payload = _structured_content_from_artifact(artifact)
        return (
            artifact_payload
            if artifact_payload is not None
            else normalize_langchain_tool_output(content)
        )

    if not isinstance(value, (str, bytes, dict, list, tuple)):
        if str(getattr(value, "type", "")).lower() == "text" and hasattr(value, "text"):
            return _parse_json_text(str(value.text or ""))
        content = getattr(value, "content", None)
        if content is not None:
            if str(getattr(value, "status", "")).lower() == "error":
                return {
                    "status": "failed",
                    "error_type": "mcp_error",
                    "error_message": _normalized_tool_error_message(content),
                }
            return normalize_langchain_tool_output(content)

    if isinstance(value, dict):
        structured = value.get("structuredContent") or value.get("structured_content")
        if isinstance(structured, dict):
            return structured
        if value.get("type") == "text" and "text" in value:
            return _parse_json_text(str(value.get("text") or ""))
        return value

    if isinstance(value, list):
        text = _content_blocks_text(value)
        if text is not None:
            return _parse_json_text(text)
        return [normalize_langchain_tool_output(item) for item in value]

    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return _parse_json_text(value)
    return value


def _structured_content_from_artifact(artifact: Any) -> dict[str, Any] | None:
    if artifact is None:
        return None
    if isinstance(artifact, dict):
        structured = artifact.get("structured_content") or artifact.get("structuredContent")
    else:
        structured = getattr(artifact, "structured_content", None)
    return dict(structured) if isinstance(structured, dict) else None


def _content_blocks_text(value: list[Any]) -> str | None:
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
            continue
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
            continue
        return None
    return "\n".join(parts)


def _parse_json_text(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return value


def _normalized_tool_error_message(content: Any) -> str:
    normalized = normalize_langchain_tool_output(content)
    if isinstance(normalized, dict):
        return (
            extract_tool_error_message(normalized)
            or str(normalized.get("summary") or "")
            or "MCP tool returned an error result"
        )
    if isinstance(normalized, list):
        parts = [str(item) for item in normalized if item not in (None, "")]
        return "\n".join(parts) or "MCP tool returned an error result"
    text = str(normalized or "").strip()
    return text or "MCP tool returned an error result"


def filter_tool_args(tool: Any, input_args: dict[str, Any]) -> dict[str, Any]:
    """Reject unknown arguments instead of silently changing an MCP invocation."""
    schema = getattr(tool, "args_schema", None)
    fields = getattr(schema, "model_fields", None) or getattr(schema, "__fields__", None)
    if fields:
        accepted = set(fields)
    elif isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        accepted = set(schema["properties"])
    else:
        tool_args = getattr(tool, "args", None)
        accepted = set(tool_args) if isinstance(tool_args, dict) else set()
    if not accepted:
        return dict(input_args)
    unknown = sorted(key for key in input_args if key not in accepted)
    if unknown:
        tool_name = str(getattr(tool, "name", "") or "unknown")
        raise ValueError(f"Tool {tool_name} received unsupported arguments: {', '.join(unknown)}")
    return dict(input_args)


def tool_map(tools: list[Any] | None) -> dict[str, Any]:
    """Index heterogeneous tool objects by their public name."""
    indexed: dict[str, Any] = {}
    for tool in tools or []:
        name = str(getattr(tool, "name", "") or "")
        if not name:
            continue
        if name in indexed:
            raise ValueError(f"Duplicate discovered tool name: {name}")
        indexed[name] = tool
    return indexed


def select_named_tools(
    tools: list[Any] | None,
    allowed_names: set[str] | frozenset[str],
) -> list[Any]:
    """Return a duplicate-checked allowlist subset of discovered tools."""
    indexed = tool_map(tools)
    return [indexed[name] for name in sorted(allowed_names) if name in indexed]


def elapsed_ms(started_at: float) -> float:
    """Return elapsed milliseconds from a perf_counter start."""
    return round((time.perf_counter() - started_at) * 1000, 2)


def clamp_int(value: Any, *, default: int, minimum: int = 1, maximum: int = 100) -> int:
    """Coerce an integer-like tool input into a bounded range."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def clamp_duration(
    value: Any,
    *,
    default: str,
    minimum_seconds: int = 1,
    maximum_seconds: int = 3600,
) -> str:
    """Normalize simple duration strings such as 10m, 1h, or 30s."""
    text = str(value or default).strip().lower()
    match = _DURATION_RE.match(text)
    if not match:
        text = default.strip().lower()
        match = _DURATION_RE.match(text)
    if not match:
        return "1m"

    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = amount * _DURATION_MULTIPLIERS[unit]
    if seconds < minimum_seconds:
        return _format_duration(minimum_seconds)
    if seconds > maximum_seconds:
        return _format_duration(maximum_seconds)
    return f"{amount}{unit}"


def _format_duration(seconds: int) -> str:
    for unit, multiplier in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= multiplier and seconds % multiplier == 0:
            return f"{seconds // multiplier}{unit}"
    return f"{max(seconds, 1)}s"


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


def failure_kind_from_output(output: Any) -> str:
    """Return the stable failure category advertised by a structured tool output."""
    if not isinstance(output, dict):
        return "structured_failure"
    return str(
        output.get("error_type") or output.get("failure_kind") or "structured_failure"
    ).lower()


def _normalized_retry_policy(value: ToolRetryPolicy | dict[str, Any]) -> ToolRetryPolicy:
    return value if isinstance(value, ToolRetryPolicy) else ToolRetryPolicy(**dict(value or {}))


def _should_retry(
    *,
    read_only: bool,
    attempt: int,
    max_attempts: int,
    failure_kind: str,
    retry_on: set[str],
    output: Any = None,
) -> bool:
    if not read_only or attempt >= max_attempts or failure_kind not in retry_on:
        return False
    if isinstance(output, dict) and output.get("retryable") is False:
        return False
    return True


def _retry_stop_reason(
    *,
    attempt: int,
    max_attempts: int,
    failure_kind: str,
    retry_on: set[str],
) -> str:
    if attempt >= max_attempts and max_attempts > 1:
        return "attempts_exhausted"
    if failure_kind not in retry_on:
        return "non_retryable_failure"
    return "retry_disabled"


async def _sleep_within_budget(backoff_seconds: float, deadline: float) -> bool:
    if backoff_seconds <= 0:
        return asyncio.get_running_loop().time() < deadline
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= backoff_seconds:
        return False
    await asyncio.sleep(backoff_seconds)
    return True
