"""Durable AIOps diagnosis session snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.incident import utc_now


class AIOpsSessionSnapshot(BaseModel):
    """Latest durable snapshot for one AIOps diagnosis session."""

    session_id: str
    incident_id: str
    trace_id: str
    status: str = "running"
    node_name: str = "workflow"
    input: str = ""
    incident: dict[str, Any] = Field(default_factory=dict)
    plan: list[dict[str, Any]] = Field(default_factory=list)
    current_plan: list[dict[str, Any]] = Field(default_factory=list)
    executed_steps: list[dict[str, Any]] = Field(default_factory=list)
    past_steps: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_records: list[dict[str, Any]] = Field(default_factory=list)
    gathered_evidence: list[dict[str, Any]] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    evidence_analysis: dict[str, Any] = Field(default_factory=dict)
    risk_assessment: dict[str, Any] = Field(default_factory=dict)
    pending_approval: dict[str, Any] | None = None
    change_plan: dict[str, Any] | None = None
    final_diagnosis: str = ""
    remediation_suggestion: str = ""
    report: dict[str, Any] | None = None
    final_report_id: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def from_state(
        cls,
        *,
        session_id: str,
        state: dict[str, Any],
        status: str = "running",
        node_name: str = "workflow",
    ) -> AIOpsSessionSnapshot:
        """Build a durable snapshot from a LangGraph state dict."""
        incident = _to_dict(state.get("incident"))
        report = _to_optional_dict(state.get("report"))
        pending_approval = _to_optional_dict(state.get("pending_approval"))
        risk_assessment = _to_dict(state.get("risk_assessment"))
        change_plan = _to_optional_dict(state.get("change_plan"))
        trace_id = str(state.get("trace_id") or incident.get("trace_id") or "trace-unknown")
        incident_id = str(
            incident.get("incident_id") or state.get("incident_id") or "incident-unknown"
        )
        final_report_id = None
        if report:
            final_report_id = str(report.get("report_id") or "") or None

        return cls(
            session_id=session_id,
            incident_id=incident_id,
            trace_id=trace_id,
            status=status,
            node_name=node_name,
            input=str(state.get("input") or ""),
            incident=incident,
            plan=_to_dict_list(state.get("plan")),
            current_plan=_to_dict_list(state.get("current_plan")),
            executed_steps=_to_dict_list(state.get("executed_steps")),
            past_steps=_normalize_past_steps(state.get("past_steps")),
            tool_call_records=_to_dict_list(state.get("tool_call_records")),
            gathered_evidence=_to_dict_list(state.get("gathered_evidence")),
            hypotheses=_to_string_list(state.get("hypotheses")),
            evidence_analysis=_to_dict(state.get("evidence_analysis")),
            risk_assessment=risk_assessment,
            pending_approval=pending_approval,
            change_plan=change_plan,
            final_diagnosis=str(state.get("final_diagnosis") or ""),
            remediation_suggestion=str(state.get("remediation_suggestion") or ""),
            report=report,
            final_report_id=final_report_id,
            errors=[str(item) for item in state.get("errors") or []],
            warnings=[str(item) for item in state.get("warnings") or []],
        )

    def to_state(self) -> dict[str, Any]:
        """Convert the durable snapshot back into a LangGraph-like state dict."""
        return {
            "session_id": self.session_id,
            "input": self.input,
            "incident": dict(self.incident or {"incident_id": self.incident_id}),
            "trace_id": self.trace_id,
            "plan": list(self.plan),
            "current_plan": list(self.current_plan),
            "executed_steps": list(self.executed_steps),
            "past_steps": list(self.past_steps),
            "tool_call_records": list(self.tool_call_records),
            "gathered_evidence": list(self.gathered_evidence),
            "hypotheses": list(self.hypotheses),
            "evidence_analysis": dict(self.evidence_analysis),
            "risk_assessment": dict(self.risk_assessment),
            "pending_approval": dict(self.pending_approval) if self.pending_approval else None,
            "change_plan": dict(self.change_plan) if self.change_plan else None,
            "final_diagnosis": self.final_diagnosis,
            "remediation_suggestion": self.remediation_suggestion,
            "report": dict(self.report) if self.report else None,
            "response": str((self.report or {}).get("markdown") or ""),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return str(value)


def _to_dict(value: Any) -> dict[str, Any]:
    safe = _json_safe(value)
    return safe if isinstance(safe, dict) else {}


def _to_optional_dict(value: Any) -> dict[str, Any] | None:
    payload = _to_dict(value)
    return payload or None


def _to_dict_list(value: Any) -> list[dict[str, Any]]:
    safe = _json_safe(value)
    if not isinstance(safe, list):
        return []
    return [item if isinstance(item, dict) else {"value": item} for item in safe]


def _to_string_list(value: Any) -> list[str]:
    safe = _json_safe(value)
    if not isinstance(safe, list):
        return []
    result: list[str] = []
    for item in safe:
        if isinstance(item, dict) and set(item.keys()) == {"value"}:
            item = item.get("value")
        if item is not None:
            result.append(str(item))
    return result


def _normalize_past_steps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            normalized.append({"step": _json_safe(item[0]), "result": _json_safe(item[1])})
        elif isinstance(item, dict):
            normalized.append(_to_dict(item))
        else:
            normalized.append({"value": _json_safe(item)})
    return normalized
