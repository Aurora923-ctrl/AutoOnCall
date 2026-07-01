"""Structured plan models for AIOps diagnosis workflows."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.incident import new_model_id


class PlanStep(BaseModel):
    """A machine-consumable diagnostic step."""

    step_id: str = Field(default_factory=lambda: new_model_id("step"))
    tool_name: str = "manual_analysis"
    purpose: str = "Execute diagnostic step"
    input_args: dict[str, Any] = Field(default_factory=dict)
    expected_evidence: str = ""
    risk_level: Literal["low", "medium", "high"] = "low"
    status: Literal["pending", "running", "success", "failed", "skipped"] = "pending"
    retry_count: int = 0
