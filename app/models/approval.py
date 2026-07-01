"""Risk assessment and human approval models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.change_plan import ChangePlan
from app.models.incident import new_model_id, utc_now


class RiskAssessment(BaseModel):
    """Risk decision for a proposed diagnostic or remediation action."""

    risk_level: Literal["low", "medium", "high"] = "low"
    action: str
    reason: str = ""
    need_approval: bool = False
    policy: Literal["allow", "approval_required", "forbidden"] = "allow"
    allowed: bool = True
    forbidden: bool = False
    matched_rules: list[str] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    """Human approval request for non-read-only or risky actions."""

    approval_id: str = Field(default_factory=lambda: new_model_id("apr"))
    incident_id: str
    action: str
    risk_level: Literal["low", "medium", "high"]
    reason: str = ""
    status: Literal["pending", "approved", "rejected", "cancelled"] = "pending"
    step_id: str | None = None
    tool_name: str | None = None
    change_plan: ChangePlan | None = None
    requested_by: str = "aiops-agent"
    decided_by: str | None = None
    decision_reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    decided_at: datetime | None = None


class ApprovalDecisionRequest(BaseModel):
    """API payload for approving or rejecting a pending action."""

    approval_id: str | None = None
    decision: Literal["approve", "reject"]
    decided_by: str = "operator"
    reason: str = ""
