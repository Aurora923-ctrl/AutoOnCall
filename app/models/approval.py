"""Risk assessment and human approval models."""

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models.change_plan import ChangePlan
from app.models.incident import new_model_id, utc_now

MAX_APPROVAL_METADATA_CHARS = 20_000


class RiskAssessment(BaseModel):
    """Risk decision for a proposed diagnostic or remediation action."""

    risk_level: Literal["low", "medium", "high"] = "low"
    action: str = Field(max_length=1000)
    reason: str = Field(default="", max_length=4000)
    need_approval: bool = False
    policy: Literal["allow", "approval_required", "forbidden"] = "allow"
    allowed: bool = True
    forbidden: bool = False
    matched_rules: list[str] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    """Human approval request for non-read-only or risky actions."""

    approval_id: str = Field(
        default_factory=lambda: new_model_id("apr"),
        min_length=1,
        max_length=128,
    )
    incident_id: str = Field(min_length=1, max_length=128)
    action: str = Field(max_length=1000)
    risk_level: Literal["low", "medium", "high"]
    reason: str = Field(default="", max_length=4000)
    status: Literal["pending", "approved", "rejected", "cancelled"] = "pending"
    step_id: str | None = Field(default=None, max_length=128)
    tool_name: str | None = Field(default=None, max_length=120)
    change_plan: ChangePlan | None = None
    requested_by: str = Field(default="aiops-agent", max_length=120)
    decided_by: str | None = Field(default=None, max_length=120)
    decision_reason: str = Field(default="", max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    decided_at: datetime | None = None

    @field_validator("metadata")
    @classmethod
    def metadata_must_be_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized) > MAX_APPROVAL_METADATA_CHARS:
            raise ValueError("approval metadata is too large")
        return value


class ApprovalDecisionRequest(BaseModel):
    """API payload for approving or rejecting a pending action."""

    approval_id: str | None = Field(default=None, max_length=128)
    decision: Literal["approve", "reject"]
    decided_by: str = Field(default="operator", max_length=120)
    reason: str = Field(default="", max_length=2000)
