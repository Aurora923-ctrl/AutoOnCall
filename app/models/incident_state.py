"""Durable incident lifecycle state for AIOps workflows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.incident import utc_now


class IncidentState(BaseModel):
    """Latest lifecycle state for one incident."""

    incident_id: str
    status: str = "created"
    status_reason: str = ""
    title: str = "AIOps incident"
    service_name: str = "unknown-service"
    severity: str = "unknown"
    environment: str = "unknown"
    summary: str = ""
    root_cause: str = ""
    trace_id: str = ""
    session_id: str = ""
    report_id: str | None = None
    approval_status: str = "not_required"
    latest_approval_id: str | None = None
    manual_action_required: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
