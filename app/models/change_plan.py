"""Human-executed change plan model for risky AIOps actions."""

import json
from datetime import datetime
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.incident import new_model_id, utc_now

ChangePlanStatus = Literal["draft", "approved", "rejected", "cancelled"]
ChangeStepStatus = Literal["pending", "running", "succeeded", "failed", "blocked", "skipped"]


class ChangeStep(BaseModel):
    """Structured execution or rollback step inside a human-approved change plan."""

    step_id: str = Field(default_factory=lambda: new_model_id("chgstep"))
    action_type: str = "manual"
    target: str = ""
    tool_name: str = ""
    input_args: dict[str, Any] = Field(default_factory=dict)
    expected_result: str = ""
    risk_level: Literal["low", "medium", "high"] = "medium"
    requires_approval: bool = True
    can_dry_run: bool = True
    rollback_step_id: str | None = None
    status: ChangeStepStatus = "pending"


class RemediationPlaybook(BaseModel):
    """Auditable remediation playbook that never executes production changes itself."""

    summary: str = ""
    risk_policy: Literal["allow", "approval_required", "forbidden"] = "approval_required"
    approval_required: bool = True
    pre_check: list[str] = Field(default_factory=list)
    dry_run: list[str] = Field(default_factory=list)
    sandbox_or_manual_record: list[str] = Field(default_factory=list)
    rollback: list[str] = Field(default_factory=list)
    observe_metrics: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)


class ChangePlan(BaseModel):
    """A non-executing production change plan attached to approvals and reports."""

    change_plan_id: str = Field(default_factory=lambda: new_model_id("chg"))
    incident_id: str
    action: str
    risk_level: Literal["low", "medium", "high"] = "medium"
    status: ChangePlanStatus = "draft"
    pre_checklist: list[str] = Field(default_factory=list)
    execution_steps: list[str] = Field(default_factory=list)
    rollback_steps: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)
    steps: list[ChangeStep] = Field(default_factory=list)
    rollback_plan: list[ChangeStep] = Field(default_factory=list)
    remediation_playbook: RemediationPlaybook | None = None
    observe_metrics: list[str] = Field(default_factory=list)
    blast_radius: str = ""
    expires_in_seconds: int = 3600
    manual_execution_required: bool = True
    notes: str = "Agent 只生成变更计划草案，不自动执行生产动作。"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


def change_plan_fingerprint(plan: ChangePlan) -> str:
    """Return a stable digest for the exact plan content presented for approval."""
    canonical = json.dumps(
        plan.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()
