"""Shared approval workflow helpers for risky AIOps actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from app.models.approval import ApprovalRequest
from app.models.change_plan import ChangePlan
from app.services.approval_service import approval_service
from app.services.change_plan_builder import build_change_plan


def build_change_plan_from_risk_decision(
    state: Mapping[str, Any],
    decision: Any,
) -> ChangePlan:
    """Create a ChangePlan draft from the current AIOps state and risk decision."""
    return build_change_plan(
        incident_id=extract_incident_id(state),
        action=str(_decision_value(decision, "action", "需要人工确认的后续处置动作")),
        risk_level=str(_decision_value(decision, "risk_level", "medium")),
        tool_name=str(_decision_value(decision, "tool_name", "") or ""),
        service_name=str(_incident_field(state, "service_name", "unknown-service")),
        environment=str(_incident_field(state, "environment", "unknown")),
        reason=str(_decision_value(decision, "reason", "")),
        metadata={
            "trace_id": state.get("trace_id"),
            "session_id": state.get("session_id"),
            "step_id": _decision_value(decision, "step_id", None),
            "policy": _decision_value(decision, "policy", "approval_required"),
        },
    )


def create_approval_request_from_risk_decision(
    state: Mapping[str, Any],
    decision: Any,
    *,
    approval_repository: Any | None = None,
    change_plan: ChangePlan | None = None,
) -> ApprovalRequest:
    """Persist an ApprovalRequest derived from a risk decision."""
    plan = change_plan or build_change_plan_from_risk_decision(state, decision)
    request = ApprovalRequest(
        incident_id=extract_incident_id(state),
        action=str(_decision_value(decision, "action", "需要人工确认的后续处置动作")),
        risk_level=_normalize_risk_level(_decision_value(decision, "risk_level", "medium")),
        reason=str(_decision_value(decision, "reason", "")),
        step_id=_optional_str(_decision_value(decision, "step_id", None)),
        tool_name=_optional_str(_decision_value(decision, "tool_name", None)),
        change_plan=plan,
        metadata={
            "trace_id": state.get("trace_id"),
            "session_id": state.get("session_id"),
            "policy": _decision_value(decision, "policy", "approval_required"),
            "matched_rules": _decision_value(decision, "matched_rules", []),
            "read_only": _decision_value(decision, "read_only", False),
            "change_plan": plan.model_dump(mode="json"),
        },
    )
    repository = approval_repository or approval_service
    return repository.create_request(request)


def extract_incident_id(state: Mapping[str, Any]) -> str:
    """Extract incident_id from state values without assuming model instances."""
    return str(_incident_field(state, "incident_id", "incident-unknown"))


def _incident_field(state: Mapping[str, Any], field_name: str, default: str) -> Any:
    incident = state.get("incident") or {}
    if isinstance(incident, Mapping):
        return incident.get(field_name) or default
    return getattr(incident, field_name, default) or default


def _decision_value(decision: Any, field_name: str, default: Any) -> Any:
    if isinstance(decision, Mapping):
        return decision.get(field_name, default)
    return getattr(decision, field_name, default)


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _normalize_risk_level(value: Any) -> Literal["low", "medium", "high"]:
    text = str(value or "medium")
    if text in {"low", "medium", "high"}:
        return text  # type: ignore[return-value]
    return "medium"
