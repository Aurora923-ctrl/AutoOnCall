"""Safe change workflow execution models."""

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.change_plan import ChangeStep
from app.models.incident import new_model_id, utc_now

ChangeExecutionMode = Literal["dry_run_only", "manual_record", "sandbox"]
ChangeExecutionStatus = Literal[
    "created",
    "precheck_running",
    "precheck_failed",
    "dry_run_running",
    "dry_run_failed",
    "dry_run_completed",
    "waiting_manual_execution",
    "manual_execution_recorded",
    "sandbox_executing",
    "sandbox_validated",
    "observing",
    "partial_success",
    "recovery_pending",
    "rollback_recommended",
    "rolled_back",
    "rollback_failed",
    "closed",
    "escalated",
]
CheckStatus = Literal["pending", "passed", "failed", "skipped"]
ManualExecutionStatus = Literal[
    "succeeded",
    "failed",
    "partial",
    "recovery_pending",
    "rolled_back",
    "rollback_failed",
]
MAX_CHANGE_PAYLOAD_CHARS = 20_000


class PreCheckResult(BaseModel):
    """Result of validating that a change plan is still safe to continue."""

    check_id: str = Field(default_factory=lambda: new_model_id("prechk"))
    change_plan_id: str = Field(min_length=1, max_length=128)
    status: CheckStatus = "pending"
    checked_items: list[str] = Field(default_factory=list)
    failed_items: list[str] = Field(default_factory=list)
    evidence_snapshot: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=2000)
    created_at: datetime = Field(default_factory=utc_now)


class DryRunResult(BaseModel):
    """Result of validating a planned change without production mutation."""

    dry_run_id: str = Field(default_factory=lambda: new_model_id("dryrun"))
    change_plan_id: str = Field(min_length=1, max_length=128)
    status: CheckStatus = "pending"
    validated_steps: list[str] = Field(default_factory=list)
    blocked_steps: list[str] = Field(default_factory=list)
    diff_preview: list[str] = Field(default_factory=list)
    reason: str = Field(default="", max_length=2000)
    created_at: datetime = Field(default_factory=utc_now)


class ObservationResult(BaseModel):
    """Post-change observation window result."""

    observation_id: str = Field(default_factory=lambda: new_model_id("obs"))
    change_execution_id: str = Field(min_length=1, max_length=128)
    status: CheckStatus = "pending"
    window_seconds: int = 300
    metrics: dict[str, Any] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    failed_criteria: list[str] = Field(default_factory=list)
    recommendation: str = Field(default="", max_length=2000)
    created_at: datetime = Field(default_factory=utc_now)


class ChangeExecution(BaseModel):
    """Persisted state for one approved safe-change workflow."""

    change_execution_id: str = Field(default_factory=lambda: new_model_id("chgexec"))
    change_plan_id: str = Field(min_length=1, max_length=128)
    approval_id: str = Field(min_length=1, max_length=128)
    incident_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(default="", max_length=128)
    mode: ChangeExecutionMode = "dry_run_only"
    status: ChangeExecutionStatus = "created"
    pre_check: PreCheckResult | None = None
    dry_run: DryRunResult | None = None
    execution_steps: list[ChangeStep] = Field(default_factory=list)
    observation: ObservationResult | None = None
    rollback_result: dict[str, Any] = Field(default_factory=dict)
    manual_result: dict[str, Any] = Field(default_factory=dict)
    projection_pending: list[str] = Field(default_factory=list)
    created_by: str = Field(default="operator", max_length=120)
    created_by_principal_id: str = Field(default="", max_length=128)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChangeResumeRequest(BaseModel):
    """Request body for starting a safe change workflow after approval."""

    approval_id: str = Field(min_length=1, max_length=128)
    mode: ChangeExecutionMode = "dry_run_only"
    operator: str = Field(default="operator", max_length=120)
    observe_window_seconds: int = Field(default=300, ge=1, le=3600)


class ManualStepResult(BaseModel):
    """Human-recorded outcome for one approved change step."""

    step_id: str = Field(min_length=1, max_length=128)
    status: Literal["succeeded", "failed", "skipped", "rolled_back"]
    notes: str = Field(default="", max_length=2000)


class ManualExecutionResultRequest(BaseModel):
    """Request body for recording a human-executed change result."""

    status: ManualExecutionStatus
    operator: str = Field(default="operator", max_length=120)
    notes: str = Field(default="", max_length=4000)
    evidence: dict[str, Any] = Field(default_factory=dict)
    observed_metrics: dict[str, Any] = Field(default_factory=dict)
    step_results: list[ManualStepResult] = Field(default_factory=list)
    observe_window_seconds: int = Field(default=300, ge=1, le=3600)

    @field_validator("evidence", "observed_metrics")
    @classmethod
    def manual_payload_must_be_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized) > MAX_CHANGE_PAYLOAD_CHARS:
            raise ValueError("manual execution payload is too large")
        return value

    @model_validator(mode="after")
    def successful_result_requires_evidence(self) -> "ManualExecutionResultRequest":
        if self.status in {"failed", "rollback_failed"}:
            return self
        if not self.notes.strip():
            raise ValueError("manual execution result requires operator notes")
        if not self.evidence:
            raise ValueError("manual execution result requires execution evidence")
        if not self.observed_metrics:
            raise ValueError("manual execution result requires observed metrics")
        return self
