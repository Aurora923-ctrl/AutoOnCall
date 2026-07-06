"""Incident domain models used by the AIOps workflow."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

MAX_INCIDENT_RAW_ALERT_CHARS = 20_000


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def new_model_id(prefix: str) -> str:
    """Create a compact prefixed identifier for demo/local workflows."""
    return f"{prefix}-{uuid4().hex}"


class Incident(BaseModel):
    """A structured incident event that drives an AIOps diagnosis."""

    incident_id: str = Field(
        default_factory=lambda: new_model_id("inc"),
        min_length=1,
        max_length=128,
    )
    title: str = Field(default="AIOps diagnosis request", min_length=1, max_length=200)
    service_name: str = Field(default="unknown-service", min_length=1, max_length=120)
    severity: str = Field(default="P2", min_length=1, max_length=32)
    symptom: str = Field(default="", max_length=4000)
    start_time: datetime = Field(default_factory=utc_now)
    environment: str = Field(default="unknown", min_length=1, max_length=80)
    raw_alert: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="investigating", min_length=1, max_length=64)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator(
        "incident_id",
        "title",
        "service_name",
        "severity",
        "symptom",
        "environment",
        "status",
        mode="before",
    )
    @classmethod
    def strip_text_fields(cls, value: Any) -> Any:
        """Trim text fields before Pydantic length validation."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("raw_alert")
    @classmethod
    def raw_alert_must_be_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized) > MAX_INCIDENT_RAW_ALERT_CHARS:
            raise ValueError("raw_alert is too large")
        return value
