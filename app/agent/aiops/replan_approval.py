"""Risk and approval handling for Replanner decisions."""

from __future__ import annotations

from typing import Any

from app.models.plan import PlanStep
from app.services.approval_workflow import (
    build_change_plan_from_risk_decision,
    create_approval_request_from_risk_decision,
    generate_approval_waiting_response,
    generate_forbidden_response,
)
from app.services.trace_service import trace_service
from app.tools.registry import create_default_tool_registry

from .risk_controller import RiskControlDecision, assess_plan_step
from .state import PlanExecuteState


def build_approval_state_update(
    state: PlanExecuteState,
    reason: str,
    *,
    force: bool = False,
    approval_repository: Any,
    trace_repository: Any = trace_service,
    risk_extractor: Any = None,
) -> dict[str, Any]:
    """Build structured risk and approval state for a paused action."""
    risk_decision = (risk_extractor or extract_risk_decision)(state)
    if risk_decision is None:
        if not force:
            return {}
        normalized_reason = reason or "后续动作可能影响线上系统，需要人工审批后再继续"
        risk_decision = RiskControlDecision(
            action="需要人工确认的后续处置动作",
            risk_level="medium",
            policy="approval_required",
            need_approval=True,
            allowed=False,
            reason=normalized_reason,
            matched_rules=["replanner:forced-approval"],
        )
    elif reason and reason not in risk_decision.reason:
        risk_decision.reason = f"{risk_decision.reason}；{reason}"

    risk_assessment = risk_decision.to_risk_assessment()
    change_plan = build_change_plan_from_risk_decision(state, risk_decision)
    trace_repository.record_risk_decision(
        trace_id=state.get("trace_id") or "trace-unknown",
        incident_id=_extract_incident_id(state),
        step_id=risk_decision.step_id,
        action=risk_decision.action,
        policy=risk_decision.policy,
        risk_level=risk_decision.risk_level,
        reason=risk_decision.reason,
        matched_rules=risk_decision.matched_rules,
        status="blocked",
    )
    update: dict[str, Any] = {
        "risk_assessment": risk_assessment.model_dump(mode="json"),
        "change_plan": change_plan.model_dump(mode="json"),
    }

    if risk_decision.policy == "forbidden":
        update["pending_approval"] = None
        update["response"] = generate_forbidden_response(risk_decision)
        update["errors"] = [risk_decision.reason]
        return update

    approval = create_approval_request_from_risk_decision(
        state,
        risk_decision,
        approval_repository=approval_repository,
        change_plan=change_plan,
    )
    update["pending_approval"] = approval.model_dump(mode="json")
    update["response"] = generate_approval_waiting_response(update)
    return update


def extract_risk_decision(
    state: PlanExecuteState,
    *,
    registry_factory: Any = create_default_tool_registry,
    assessor: Any = assess_plan_step,
) -> RiskControlDecision | None:
    """Infer risk from remaining structured plan steps."""
    registry = registry_factory([])
    current_plan = state.get("current_plan", [])
    if not current_plan and state.get("plan"):
        return RiskControlDecision(
            action="Legacy text-only plan",
            risk_level="high",
            read_only=False,
            policy="forbidden",
            need_approval=True,
            allowed=False,
            forbidden=True,
            reason="Legacy text-only plan cannot be risk assessed safely",
            matched_rules=["plan:legacy-unassessed"],
        )
    for raw_step in current_plan:
        try:
            step = raw_step if isinstance(raw_step, PlanStep) else PlanStep(**raw_step)
        except Exception:
            return RiskControlDecision(
                action="Invalid structured plan step",
                risk_level="high",
                read_only=False,
                policy="forbidden",
                need_approval=True,
                allowed=False,
                forbidden=True,
                reason="Remaining plan contains an invalid structured step and cannot be assessed safely",
                matched_rules=["plan:invalid-step"],
            )
        decision = assessor(
            step,
            tool_registry=registry,
            incident=state.get("incident"),
        )
        if decision.policy != "allow":
            return decision
    return None


def _extract_incident_id(state: PlanExecuteState) -> str:
    incident = state.get("incident")
    if isinstance(incident, dict):
        return str(incident.get("incident_id") or "")
    return str(getattr(incident, "incident_id", "") or "")
