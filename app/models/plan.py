"""Structured plan models for AIOps diagnosis workflows."""

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models.incident import new_model_id

MAX_PLAN_INPUT_ARGS_CHARS = 20_000


class PlanStep(BaseModel):
    """A machine-consumable diagnostic step."""

    step_id: str = Field(default_factory=lambda: new_model_id("step"), min_length=1, max_length=128)
    tool_name: str = Field(default="manual_analysis", min_length=1, max_length=120)
    purpose: str = Field(default="Execute diagnostic step", min_length=1, max_length=1000)
    input_args: dict[str, Any] = Field(default_factory=dict)
    expected_evidence: str = Field(default="", max_length=1000)
    risk_level: Literal["low", "medium", "high"] = "low"
    status: Literal["pending", "running", "success", "failed", "skipped"] = "pending"
    retry_count: int = Field(default=0, ge=0, le=3)

    @field_validator("step_id", "tool_name", "purpose", mode="before")
    @classmethod
    def required_text_must_not_be_blank(cls, value: Any) -> Any:
        """Trim required identifiers and reject whitespace-only plan fields."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("input_args")
    @classmethod
    def input_args_must_be_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized) > MAX_PLAN_INPUT_ARGS_CHARS:
            raise ValueError("plan input_args is too large")
        return value
