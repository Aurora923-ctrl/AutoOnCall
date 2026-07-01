"""Typed API response contracts for AIOps read and approval endpoints."""

from typing import Any

from pydantic import BaseModel, Field

from app.models.approval import ApprovalRequest
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent


class ApprovalListResponse(BaseModel):
    """List response for pending approval requests."""

    items: list[ApprovalRequest] = Field(default_factory=list)


class IncidentApprovalListResponse(ApprovalListResponse):
    """List response for approval requests scoped to one incident."""

    incident_id: str


class ApprovalDecisionResponse(BaseModel):
    """Response after approving or rejecting one approval request."""

    approval: ApprovalRequest


class IncidentListResponse(BaseModel):
    """List response for incident overview summaries."""

    items: list[dict[str, Any]] = Field(default_factory=list)


class IncidentOverviewResponse(BaseModel):
    """Aggregated incident, trace, approval, report, and diagnosis-chain view."""

    incident_id: str
    trace_id: str = ""
    status: str
    status_metadata: dict[str, Any] = Field(default_factory=dict)
    status_reason: str = ""
    title: str
    service_name: str
    severity: str
    environment: str
    summary: str = ""
    root_cause: str = ""
    manual_action_required: bool = False
    approval_status: str = "not_required"
    session_id: str = ""
    lifecycle: dict[str, Any] | None = None
    trace_summary: dict[str, Any] = Field(default_factory=dict)
    approval_summary: dict[str, Any] = Field(default_factory=dict)
    report: DiagnosisReport | None = None
    diagnosis_chain: dict[str, Any] = Field(default_factory=dict)
    links: dict[str, str] = Field(default_factory=dict)
    updated_at: str = ""


class IncidentTraceResponse(BaseModel):
    """Trace response for one incident."""

    incident_id: str
    trace_id: str = ""
    items: list[TraceEvent] = Field(default_factory=list)


class IncidentReportResponse(BaseModel):
    """Latest diagnosis report response for one incident."""

    incident_id: str
    trace_id: str = ""
    report_id: str = ""
    report: DiagnosisReport | None = None
    markdown: str = ""
