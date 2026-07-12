"""Trace and tool call audit models for AIOps workflows."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.incident import new_model_id, utc_now


class ToolCallRecord(BaseModel):
    """Audit record for one tool invocation."""

    call_id: str = Field(default_factory=lambda: new_model_id("call"))
    trace_id: str
    incident_id: str
    step_id: str
    tool_name: str
    input_args: dict[str, Any] = Field(default_factory=dict)
    input_summary: str = ""
    output: Any = None
    output_summary: str = ""
    output_artifact: dict[str, Any] | None = None
    data_source: str = "unknown"
    latency_ms: float = 0.0
    status: str = "pending"
    risk_level: str = "low"
    read_only: bool = True
    error_message: str | None = None
    execution_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class TraceEvent(BaseModel):
    """Trace event for graph nodes, tool calls, and future LLM calls."""

    event_id: str = Field(default_factory=lambda: new_model_id("traceevt"))
    trace_id: str
    incident_id: str
    event_type: str = "node"
    node_name: str
    step_id: str | None = None
    input_summary: str = ""
    output_summary: str = ""
    tool_name: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    tool_result: Any = None
    latency_ms: float = 0.0
    status: str = "success"
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
