"""Durable incident lifecycle state for AIOps workflows."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.models.incident import utc_now

MAX_INCIDENT_STATE_METADATA_CHARS = 20_000


class IncidentState(BaseModel):
    """Latest lifecycle state for one incident."""

    incident_id: str = Field(min_length=1, max_length=128)
    status: str = Field(default="created", max_length=64)
    status_reason: str = Field(default="", max_length=2000)
    title: str = Field(default="AIOps incident", max_length=200)
    service_name: str = Field(default="unknown-service", max_length=120)
    severity: str = Field(default="unknown", max_length=32)
    environment: str = Field(default="unknown", max_length=80)
    summary: str = Field(default="", max_length=4000)
    root_cause: str = Field(default="", max_length=4000)
    trace_id: str = Field(default="", max_length=128)
    session_id: str = Field(default="", max_length=128)
    report_id: str | None = Field(default=None, max_length=128)
    approval_status: str = Field(default="not_required", max_length=64)
    latest_approval_id: str | None = Field(default=None, max_length=128)
    manual_action_required: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def metadata_must_be_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized) > MAX_INCIDENT_STATE_METADATA_CHARS:
            raise ValueError("incident state metadata is too large")
        return value
