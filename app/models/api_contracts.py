"""Typed API response contracts for AIOps read and approval endpoints."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.approval import ApprovalRequest
from app.models.feedback import BadCaseFeedback, DiagnosisFeedback, EvalBacklogItem
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


class IncidentReplayResponse(BaseModel):
    """Replay-ready view of one diagnosis from alert to report."""

    incident_id: str
    trace_id: str = ""
    status: str
    status_metadata: dict[str, Any] = Field(default_factory=dict)
    title: str
    service_name: str
    severity: str
    environment: str
    summary: str = ""
    root_cause: str = ""
    overview: dict[str, Any] = Field(default_factory=dict)
    stages: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    replanner_decisions: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_quality: dict[str, Any] = Field(default_factory=dict)
    tooling: dict[str, Any] = Field(default_factory=dict)
    approval_flow: dict[str, Any] = Field(default_factory=dict)
    change_flow: dict[str, Any] = Field(default_factory=dict)
    report_summary: dict[str, Any] = Field(default_factory=dict)
    evaluation: dict[str, Any] = Field(default_factory=dict)
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


class IncidentFeedbackResponse(BaseModel):
    """Response after an operator submits report feedback."""

    feedback: DiagnosisFeedback


class IncidentFeedbackListResponse(BaseModel):
    """Feedback items scoped to one incident."""

    incident_id: str
    items: list[DiagnosisFeedback] = Field(default_factory=list)


class BadCaseFeedbackResponse(BaseModel):
    """Response after thumb feedback is captured."""

    feedback: BadCaseFeedback


class BadCaseFeedbackListResponse(BaseModel):
    """List response for captured bad cases."""

    items: list[BadCaseFeedback] = Field(default_factory=list)


class EvalBacklogListResponse(BaseModel):
    """List response for reviewable eval-backlog drafts."""

    items: list[EvalBacklogItem] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class EvalBacklogResponse(BaseModel):
    """Read response for reviewable eval-backlog drafts."""

    available: bool = False
    summary: dict[str, Any] = Field(default_factory=dict)
    items: list[EvalBacklogItem] = Field(default_factory=list)
    invalid_items: list[dict[str, Any]] = Field(default_factory=list)


class EvalRagasResponse(BaseModel):
    """Read response for the latest optional RAGAS quality report."""

    available: bool = False
    path: str = ""
    artifact: str = ""
    run: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    thresholds: dict[str, Any] = Field(default_factory=dict)
    quality_contract: dict[str, Any] = Field(default_factory=dict)
    dashboard: dict[str, Any] = Field(default_factory=dict)
    case_scores: list[dict[str, Any]] = Field(default_factory=list)
    failed_cases: list[dict[str, Any]] = Field(default_factory=list)
    artifact_status: dict[str, Any] = Field(default_factory=dict)
    stale: bool = True
    message: str = ""


class KnowledgeIndexingReportsResponse(BaseModel):
    """Knowledge indexing quality report response."""

    code: int = 200
    message: str = "success"
    data: dict[str, Any] = Field(default_factory=dict)


class UploadConfigData(BaseModel):
    """Frontend-visible file upload constraints."""

    allowed_extensions: list[str] = Field(default_factory=list)
    max_file_size: int = Field(ge=1)
    max_file_size_mb: int = Field(ge=1)


class UploadConfigResponse(BaseModel):
    """File upload configuration response."""

    code: int = 200
    message: str = "success"
    data: UploadConfigData


class UploadIndexingStatus(BaseModel):
    """Public indexing outcome returned by the upload API."""

    status: Literal["success", "empty", "failed"]
    chunk_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    error_message: str | None = None
    message: str | None = None
    cleaning: dict[str, Any] = Field(default_factory=dict)


class UploadFileData(BaseModel):
    """Saved file metadata plus indexing readiness."""

    filename: str
    file_path: str
    size: int = Field(ge=0)
    overwritten: bool = False
    indexing_ready: bool = False
    indexing: UploadIndexingStatus


class UploadFileResponse(BaseModel):
    """File upload and indexing response."""

    code: int
    message: str
    data: UploadFileData
