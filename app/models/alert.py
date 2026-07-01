"""Alert ingestion models for external monitoring webhooks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.incident import utc_now


class AlertEvent(BaseModel):
    """Normalized alert event persisted before it becomes an Incident."""

    source: str = "alertmanager"
    fingerprint: str
    incident_id: str
    status: str = "firing"
    alertname: str = "UnknownAlert"
    service_name: str = "unknown-service"
    severity: str = "P2"
    environment: str = "unknown"
    summary: str = ""
    description: str = ""
    labels: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    generator_url: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AlertIngestionItem(BaseModel):
    """One normalized alert and the Incident lifecycle result it produced."""

    event: AlertEvent
    created: bool = False
    deduplicated: bool = False
    incident_id: str
    incident_status: str
    status_reason: str = ""


class AlertIngestionResult(BaseModel):
    """Response returned after processing an alert webhook."""

    source: str = "alertmanager"
    received: int = 0
    created: int = 0
    deduplicated: int = 0
    resolved: int = 0
    items: list[AlertIngestionItem] = Field(default_factory=list)


class AlertListResponse(BaseModel):
    """List response for normalized alert events."""

    items: list[AlertEvent] = Field(default_factory=list)


class AlertDetailResponse(BaseModel):
    """Detail response for one normalized alert event."""

    alert: AlertEvent
