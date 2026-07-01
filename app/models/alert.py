"""Alert ingestion models for external monitoring webhooks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.incident import utc_now

MAX_ALERT_SOURCE_LENGTH = 64
MAX_ALERT_FINGERPRINT_LENGTH = 128
MAX_ALERT_INCIDENT_ID_LENGTH = 128
MAX_ALERT_STATUS_LENGTH = 32
MAX_ALERT_NAME_LENGTH = 256
MAX_ALERT_SERVICE_NAME_LENGTH = 128
MAX_ALERT_SEVERITY_LENGTH = 16
MAX_ALERT_ENVIRONMENT_LENGTH = 64
MAX_ALERT_TEXT_LENGTH = 4096
MAX_ALERT_URL_LENGTH = 2048


class AlertEvent(BaseModel):
    """Normalized alert event persisted before it becomes an Incident."""

    source: str = Field(default="alertmanager", max_length=MAX_ALERT_SOURCE_LENGTH)
    fingerprint: str = Field(max_length=MAX_ALERT_FINGERPRINT_LENGTH)
    incident_id: str = Field(max_length=MAX_ALERT_INCIDENT_ID_LENGTH)
    status: str = Field(default="firing", max_length=MAX_ALERT_STATUS_LENGTH)
    alertname: str = Field(default="UnknownAlert", max_length=MAX_ALERT_NAME_LENGTH)
    service_name: str = Field(default="unknown-service", max_length=MAX_ALERT_SERVICE_NAME_LENGTH)
    severity: str = Field(default="P2", max_length=MAX_ALERT_SEVERITY_LENGTH)
    environment: str = Field(default="unknown", max_length=MAX_ALERT_ENVIRONMENT_LENGTH)
    summary: str = Field(default="", max_length=MAX_ALERT_TEXT_LENGTH)
    description: str = Field(default="", max_length=MAX_ALERT_TEXT_LENGTH)
    labels: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    generator_url: str = Field(default="", max_length=MAX_ALERT_URL_LENGTH)
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AlertIngestionItem(BaseModel):
    """One normalized alert and the Incident lifecycle result it produced."""

    event: AlertEvent
    created: bool = False
    deduplicated: bool = False
    previous_status: str | None = None
    status_changed: bool = False
    reopened: bool = False
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
