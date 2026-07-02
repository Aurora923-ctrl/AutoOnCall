"""Shared approval workflow helpers for risky AIOps actions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, Literal, cast

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
    repository = approval_repository or approval_service
    incident_id = extract_incident_id(state)
    idempotency_key = build_approval_idempotency_key(state, decision)
    existing = find_pending_approval_by_idempotency_key(repository, incident_id, idempotency_key)
    if existing is not None:
        return existing

    request = ApprovalRequest(
        incident_id=incident_id,
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
            "idempotency_key": idempotency_key,
            "change_plan": plan.model_dump(mode="json"),
        },
    )
    return repository.create_request(request)


def extract_incident_id(state: Mapping[str, Any]) -> str:
    """Extract incident_id from state values without assuming model instances."""
    return str(_incident_field(state, "incident_id", "incident-unknown"))


def build_approval_idempotency_key(state: Mapping[str, Any], decision: Any) -> str:
    """Build a stable key for one pending risky action within an incident."""
    payload = {
        "incident_id": extract_incident_id(state),
        "step_id": _decision_value(decision, "step_id", None),
        "tool_name": _decision_value(decision, "tool_name", None),
        "action": _decision_value(decision, "action", ""),
        "risk_level": _decision_value(decision, "risk_level", "medium"),
        "policy": _decision_value(decision, "policy", "approval_required"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def find_pending_approval_by_idempotency_key(
    repository: Any,
    incident_id: str,
    idempotency_key: str,
) -> ApprovalRequest | None:
    """Return an existing pending approval for the same risky action, when available."""
    if not idempotency_key or not hasattr(repository, "list_pending"):
        return None
    for raw_request in repository.list_pending(incident_id=incident_id):
        request = cast(ApprovalRequest, raw_request)
        metadata = request.metadata or {}
        if metadata.get("idempotency_key") == idempotency_key:
            return request
    return None


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
