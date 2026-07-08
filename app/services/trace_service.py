"""Trace event storage for AIOps diagnosis workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models.trace import ToolCallRecord, TraceEvent
from app.services.aiops_store import create_aiops_store
from app.services.legacy_migration import resolve_legacy_jsonl_path
from app.services.sqlite_store import resolve_sqlite_path
from app.utils.redaction import redact_sensitive_data, redact_sensitive_text


class TraceService:
    """Trace repository backed by SQLite."""

    def __init__(
        self,
        storage_path: str | Path | None = None,
        *,
        legacy_storage_path: str | Path | None = None,
    ):
        raw_storage_path = Path(storage_path) if storage_path is not None else None
        self.database_path = resolve_sqlite_path(raw_storage_path)
        self._store = create_aiops_store(raw_storage_path)
        self.storage_path = getattr(self._store, "storage_path", self.database_path)
        self._migrate_legacy_jsonl(
            legacy_storage_path
            if legacy_storage_path is not None
            else resolve_legacy_jsonl_path(raw_storage_path, "traces.jsonl")
        )

    def create_event(
        self,
        *,
        trace_id: str,
        incident_id: str,
        node_name: str,
        event_type: str = "node",
        step_id: str | None = None,
        input_summary: str = "",
        output_summary: str = "",
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_result: Any = None,
        latency_ms: float = 0.0,
        status: str = "success",
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceEvent:
        """Create, store, and persist a trace event."""
        event = TraceEvent(
            trace_id=trace_id,
            incident_id=incident_id,
            node_name=node_name,
            event_type=event_type,
            step_id=step_id,
            input_summary=_truncate(redact_sensitive_text(input_summary)),
            output_summary=_truncate(redact_sensitive_text(output_summary)),
            tool_name=tool_name,
            tool_args=redact_sensitive_data(dict(tool_args or {})),
            tool_result=_compact_value(redact_sensitive_data(tool_result)),
            latency_ms=latency_ms,
            status=status,
            error_message=redact_sensitive_text(error_message) if error_message else None,
            metadata=redact_sensitive_data(dict(metadata or {})),
        )
        self._store.save_trace_event(event)
        return event

    def record_node_event(
        self,
        *,
        trace_id: str,
        incident_id: str,
        node_name: str,
        node_output: dict[str, Any] | None = None,
        status: str = "success",
        metadata: dict[str, Any] | None = None,
    ) -> TraceEvent:
        """Record a LangGraph node update."""
        output = node_output or {}
        return self.create_event(
            trace_id=trace_id,
            incident_id=incident_id,
            node_name=node_name,
            event_type="node",
            input_summary=f"node={node_name}",
            output_summary=_summarize_node_output(output),
            status=status,
            metadata={
                "output_keys": sorted(output.keys()),
                **_compact_node_metadata(output),
                **dict(metadata or {}),
            },
        )

    def record_tool_call(self, record: ToolCallRecord | dict[str, Any]) -> TraceEvent:
        """Record a tool call audit object as a trace event."""
        call = (
            record if isinstance(record, ToolCallRecord) else ToolCallRecord.model_validate(record)
        )
        return self.create_event(
            trace_id=call.trace_id,
            incident_id=call.incident_id,
            node_name="executor",
            event_type="tool_call",
            step_id=call.step_id,
            input_summary=call.input_summary or f"调用工具 {call.tool_name}",
            output_summary=(
                call.output_summary
                if call.status == "success"
                else call.error_message or call.output_summary or ""
            ),
            tool_name=call.tool_name,
            tool_args=redact_sensitive_data(call.input_args),
            tool_result=call.output,
            latency_ms=call.latency_ms,
            status=call.status,
            error_message=call.error_message,
            metadata={
                "call_id": call.call_id,
                "data_source": call.data_source,
                "risk_level": call.risk_level,
                "read_only": call.read_only,
                "output_artifact": call.output_artifact,
            },
        )

    def record_risk_decision(
        self,
        *,
        trace_id: str,
        incident_id: str,
        step_id: str | None,
        action: str,
        policy: str,
        risk_level: str,
        reason: str,
        matched_rules: list[str] | None = None,
        status: str = "success",
    ) -> TraceEvent:
        """Record a risk controller decision."""
        return self.create_event(
            trace_id=trace_id,
            incident_id=incident_id,
            node_name="risk_controller",
            event_type="risk_decision",
            step_id=step_id,
            input_summary=action,
            output_summary=f"policy={policy}, risk={risk_level}, reason={reason}",
            status=status,
            error_message=None if status == "success" else reason,
            metadata={
                "policy": policy,
                "risk_level": risk_level,
                "matched_rules": list(matched_rules or []),
            },
        )

    def record_approval_event(
        self,
        *,
        trace_id: str,
        incident_id: str,
        approval_id: str,
        event_type: str,
        action: str,
        status: str,
        reason: str = "",
        step_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceEvent:
        """Record approval request creation or decision."""
        return self.create_event(
            trace_id=trace_id,
            incident_id=incident_id,
            node_name="approval_service",
            event_type=event_type,
            step_id=step_id,
            input_summary=action,
            output_summary=f"approval_id={approval_id}, status={status}, reason={reason}",
            status=status,
            metadata={
                "approval_id": approval_id,
                **dict(metadata or {}),
            },
        )

    def record_change_event(
        self,
        *,
        trace_id: str,
        incident_id: str,
        change_execution_id: str,
        change_plan_id: str,
        approval_id: str,
        event_type: str,
        status: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
    ) -> TraceEvent:
        """Record a safe change workflow stage transition."""
        return self.create_event(
            trace_id=trace_id,
            incident_id=incident_id,
            node_name="safe_change_workflow",
            event_type=event_type,
            input_summary=f"change_execution_id={change_execution_id}",
            output_summary=summary,
            status=status,
            metadata={
                "change_execution_id": change_execution_id,
                "change_plan_id": change_plan_id,
                "approval_id": approval_id,
                **dict(metadata or {}),
            },
        )

    def list_events(
        self,
        *,
        incident_id: str | None = None,
        trace_id: str | None = None,
        event_type: str | None = None,
    ) -> list[TraceEvent]:
        """List trace events filtered by incident, trace, or type."""
        return self._store.list_trace_events(
            incident_id=incident_id,
            trace_id=trace_id,
            event_type=event_type,
        )

    def _migrate_legacy_jsonl(self, legacy_storage_path: str | Path | None) -> None:
        if legacy_storage_path is None:
            return
        path = Path(legacy_storage_path)
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                payload = record.get("trace_event") or record
                event = TraceEvent.model_validate(payload)
            except Exception:
                continue
            self._store.save_trace_event(event)


def _summarize_node_output(output: dict[str, Any]) -> str:
    parts: list[str] = []
    if "plan" in output:
        parts.append(f"plan_steps={len(output.get('plan') or [])}")
    if "current_plan" in output:
        parts.append(f"current_plan_steps={len(output.get('current_plan') or [])}")
    if "past_steps" in output:
        parts.append(f"past_steps_added={len(output.get('past_steps') or [])}")
    if output.get("pending_approval"):
        approval = output["pending_approval"]
        parts.append(f"pending_approval={approval.get('approval_id', '')}")
    if output.get("response"):
        parts.append("response_generated=true")
    if output.get("errors"):
        parts.append(f"errors={len(output.get('errors') or [])}")
    return ", ".join(parts) or _summarize_value(output)


def _compact_node_metadata(output: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if output.get("current_plan"):
        metadata["current_plan"] = output.get("current_plan")
    elif output.get("plan"):
        metadata["plan"] = output.get("plan")
    if output.get("evidence_analysis"):
        analysis = output.get("evidence_analysis")
        metadata["evidence_analysis"] = analysis if isinstance(analysis, dict) else str(analysis)
    return metadata


def _summarize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, dict):
        summary = value.get("summary")
        if summary:
            return _truncate(str(summary))
        return _truncate(json.dumps(value, ensure_ascii=False, default=str))
    if isinstance(value, list):
        return f"list[{len(value)}]"
    return _truncate(str(value))


def _compact_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return value[:20]
    if isinstance(value, dict):
        return value
    return str(value)


def _truncate(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


trace_service = TraceService()
