"""Incident domain models used by the AIOps workflow."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def new_model_id(prefix: str) -> str:
    """Create a compact prefixed identifier for demo/local workflows."""
    return f"{prefix}-{uuid4().hex}"


class Incident(BaseModel):
    """A structured incident event that drives an AIOps diagnosis."""

    incident_id: str = Field(default_factory=lambda: new_model_id("inc"))
    title: str = "AIOps diagnosis request"
    service_name: str = "unknown-service"
    severity: str = "P2"
    symptom: str = ""
    start_time: datetime = Field(default_factory=utc_now)
    environment: str = "unknown"
    raw_alert: dict[str, Any] = Field(default_factory=dict)
    status: str = "investigating"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
