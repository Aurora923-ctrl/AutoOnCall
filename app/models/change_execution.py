"""Safe change workflow execution models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

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
    "rollback_recommended",
    "closed",
    "escalated",
]
CheckStatus = Literal["pending", "passed", "failed", "skipped"]
ManualExecutionStatus = Literal["succeeded", "failed"]


class PreCheckResult(BaseModel):
    """Result of validating that a change plan is still safe to continue."""

    check_id: str = Field(default_factory=lambda: new_model_id("prechk"))
    change_plan_id: str
    status: CheckStatus = "pending"
    checked_items: list[str] = Field(default_factory=list)
    failed_items: list[str] = Field(default_factory=list)
    evidence_snapshot: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class DryRunResult(BaseModel):
    """Result of validating a planned change without production mutation."""

    dry_run_id: str = Field(default_factory=lambda: new_model_id("dryrun"))
    change_plan_id: str
    status: CheckStatus = "pending"
    validated_steps: list[str] = Field(default_factory=list)
    blocked_steps: list[str] = Field(default_factory=list)
    diff_preview: list[str] = Field(default_factory=list)
    reason: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class ObservationResult(BaseModel):
    """Post-change observation window result."""

    observation_id: str = Field(default_factory=lambda: new_model_id("obs"))
    change_execution_id: str
    status: CheckStatus = "pending"
    window_seconds: int = 300
    metrics: dict[str, Any] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    failed_criteria: list[str] = Field(default_factory=list)
    recommendation: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class ChangeExecution(BaseModel):
    """Persisted state for one approved safe-change workflow."""

    change_execution_id: str = Field(default_factory=lambda: new_model_id("chgexec"))
    change_plan_id: str
    approval_id: str
    incident_id: str
    trace_id: str = ""
    mode: ChangeExecutionMode = "dry_run_only"
    status: ChangeExecutionStatus = "created"
    pre_check: PreCheckResult | None = None
    dry_run: DryRunResult | None = None
    execution_steps: list[ChangeStep] = Field(default_factory=list)
    observation: ObservationResult | None = None
    rollback_result: dict[str, Any] = Field(default_factory=dict)
    manual_result: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "operator"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChangeResumeRequest(BaseModel):
    """Request body for starting a safe change workflow after approval."""

    approval_id: str
    mode: ChangeExecutionMode = "dry_run_only"
    operator: str = "operator"
    observe_window_seconds: int = Field(default=300, ge=1, le=3600)


class ManualExecutionResultRequest(BaseModel):
    """Request body for recording a human-executed change result."""

    status: ManualExecutionStatus
    operator: str = "operator"
    notes: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    observed_metrics: dict[str, Any] = Field(default_factory=dict)
    observe_window_seconds: int = Field(default=300, ge=1, le=3600)
