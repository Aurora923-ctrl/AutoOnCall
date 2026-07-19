"""Shared approval workflow helpers for risky AIOps actions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, Literal, cast

from app.models.approval import ApprovalRequest
from app.models.change_plan import ChangePlan, change_plan_fingerprint
from app.services.aiops_state_utils import extract_incident_id, incident_field
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
        service_name=str(incident_field(state, "service_name", "unknown-service")),
        environment=str(incident_field(state, "environment", "unknown")),
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
    idempotency_key = build_approval_idempotency_key(
        state,
        decision,
        change_plan=plan,
    )
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
            "change_plan_fingerprint": change_plan_fingerprint(plan),
        },
    )
    if hasattr(repository, "create_request_once"):
        return repository.create_request_once(request, idempotency_key=idempotency_key)

    existing = find_pending_approval_by_idempotency_key(repository, incident_id, idempotency_key)
    if existing is not None:
        return existing
    return repository.create_request(request)


def generate_approval_waiting_response(state_update: Mapping[str, Any]) -> str:
    """Generate a deterministic pause response for pending approval."""
    approval = _mapping_value(state_update.get("pending_approval"))
    risk = _mapping_value(state_update.get("risk_assessment"))
    return f"""# AIOps 诊断已暂停，等待人工审批

## 待审批动作
{approval.get("action", "需要人工确认的后续处置动作")}

## 风险等级
{risk.get("risk_level", approval.get("risk_level", "medium"))}

## 审批原因
{approval.get("reason", "后续动作可能影响线上系统，需要人工审批后再继续")}

## 人工动作边界
审批只用于确认后续人工处置建议，Agent 不会自动执行生产变更。

## 回滚建议
执行任何变更前需要确认回滚命令、影响范围和观察窗口。
"""


def generate_forbidden_response(decision: Any) -> str:
    """Generate deterministic response for forbidden actions."""
    return f"""# AIOps 已拦截危险动作

## 动作
{_decision_value(decision, "action", "")}

## 风险等级
{_decision_value(decision, "risk_level", "high")}

## 拦截原因
{_decision_value(decision, "reason", "")}

## 处理建议
请通过人工变更、工单审批或专用运维平台重新评估该动作，不允许 Agent 自动执行。若确需处理，必须先准备回滚方案。
"""


def generate_risk_stop_response(decision: Any) -> str:
    """Render the stop reason for the user-facing diagnosis stream."""
    policy = str(_decision_value(decision, "policy", ""))
    if policy == "forbidden":
        title = "AIOps 已拦截危险动作"
        next_step = "该动作不会自动执行，请由人工在变更流程中重新评估。"
    else:
        title = "AIOps 诊断已暂停，等待人工审批"
        next_step = "审批通过前，Agent 不会自动执行该动作。"

    return f"""# {title}

## 动作
{_decision_value(decision, "action", "")}

## 风险等级
{_decision_value(decision, "risk_level", "medium")}

## 策略
{policy or "approval_required"}

## 原因
{_decision_value(decision, "reason", "")}

## 下一步
{next_step}
"""


def build_approval_idempotency_key(
    state: Mapping[str, Any],
    decision: Any,
    *,
    change_plan: ChangePlan | None = None,
) -> str:
    """Build a stable key for one pending risky action within an incident."""
    plan = change_plan or build_change_plan_from_risk_decision(state, decision)
    payload = {
        "incident_id": extract_incident_id(state),
        "session_id": state.get("session_id") or state.get("trace_id") or "",
        "trace_id": state.get("trace_id") or "",
        "step_id": _decision_value(decision, "step_id", None),
        "tool_name": _decision_value(decision, "tool_name", None),
        "action": _decision_value(decision, "action", ""),
        "risk_level": _decision_value(decision, "risk_level", "medium"),
        "policy": _decision_value(decision, "policy", "approval_required"),
        "service_name": incident_field(state, "service_name", "unknown-service"),
        "environment": incident_field(state, "environment", "unknown"),
        "change_plan_scope": _change_plan_idempotency_scope(plan),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def _change_plan_idempotency_scope(plan: ChangePlan) -> dict[str, Any]:
    """Return plan content without generated identities or lifecycle timestamps."""
    volatile_fields = {
        "change_plan_id",
        "step_id",
        "rollback_step_id",
        "created_at",
        "status",
    }

    def normalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): normalize(item)
                for key, item in value.items()
                if key not in volatile_fields
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    normalized = normalize(plan.model_dump(mode="json"))
    return dict(normalized) if isinstance(normalized, dict) else {}


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


def _decision_value(decision: Any, field_name: str, default: Any) -> Any:
    if isinstance(decision, Mapping):
        return decision.get(field_name, default)
    return getattr(decision, field_name, default)


def _mapping_value(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _normalize_risk_level(value: Any) -> Literal["low", "medium", "high"]:
    text = str(value or "medium")
    if text in {"low", "medium", "high"}:
        return text  # type: ignore[return-value]
    return "medium"
