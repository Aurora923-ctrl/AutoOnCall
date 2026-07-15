"""OpenAPI contract tests for key AIOps read and approval endpoints."""

import pytest
from fastapi import FastAPI

from app.api import alerts, approvals, chat, evaluations, feedback, file, health, incidents
from app.models.api_contracts import UploadIndexingStatus


def test_incident_and_approval_routes_expose_response_models() -> None:
    app = FastAPI()
    app.include_router(health.router)
    app.include_router(chat.router, prefix="/api")
    app.include_router(alerts.router, prefix="/api")
    app.include_router(approvals.router, prefix="/api")
    app.include_router(incidents.router, prefix="/api")
    app.include_router(feedback.router, prefix="/api")
    app.include_router(evaluations.router, prefix="/api")
    app.include_router(file.router, prefix="/api")

    schema = app.openapi()
    paths = schema["paths"]

    expected_refs = {
        ("/api/alerts/alertmanager", "post"): "AlertIngestionResult",
        ("/api/alerts", "get"): "AlertListResponse",
        ("/api/alerts/{fingerprint}", "get"): "AlertDetailResponse",
        ("/api/approvals/pending", "get"): "ApprovalListResponse",
        ("/api/incidents/{incident_id}/approval", "get"): "IncidentApprovalListResponse",
        ("/api/incidents/{incident_id}/approval", "post"): "ApprovalDecisionResponse",
        ("/api/incidents", "get"): "IncidentListResponse",
        ("/api/incidents/{incident_id}", "get"): "IncidentOverviewResponse",
        ("/api/incidents/{incident_id}/replay", "get"): "IncidentReplayResponse",
        ("/api/incidents/{incident_id}/trace", "get"): "IncidentTraceResponse",
        ("/api/incidents/{incident_id}/report", "get"): "IncidentReportResponse",
        ("/api/incidents/{incident_id}/feedback", "post"): "IncidentFeedbackResponse",
        ("/api/incidents/{incident_id}/feedback", "get"): "IncidentFeedbackListResponse",
        ("/api/feedback", "post"): "BadCaseFeedbackResponse",
        ("/api/feedback/bad-cases", "get"): "BadCaseFeedbackListResponse",
        ("/api/feedback/eval-backlog", "get"): "EvalBacklogListResponse",
        ("/api/eval/backlog", "get"): "EvalBacklogResponse",
        ("/api/eval/ragas", "get"): "EvalRagasResponse",
        ("/api/eval/ragas-summary", "get"): "EvalRagasResponse",
        ("/api/knowledge/indexing/reports", "get"): "KnowledgeIndexingReportsResponse",
        ("/api/upload/config", "get"): "UploadConfigResponse",
        ("/api/upload", "post"): "UploadFileResponse",
        ("/api/chat", "post"): "ChatApiResponse",
        ("/health/live", "get"): "HealthApiResponse",
        ("/health/ready", "get"): "HealthApiResponse",
    }

    for (path, method), schema_name in expected_refs.items():
        response_schema = paths[path][method]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert response_schema["$ref"] == f"#/components/schemas/{schema_name}"

    upload_partial_schema = paths["/api/upload"]["post"]["responses"]["207"]["content"][
        "application/json"
    ]["schema"]
    assert upload_partial_schema["$ref"] == "#/components/schemas/UploadFileResponse"


def test_upload_indexing_contract_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        UploadIndexingStatus(status="unknown", chunk_count=0, duration_ms=0)
