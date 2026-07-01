"""Tests for durable AIOps read-model status resolution."""

from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.approval import ApprovalRequest
from app.models.report import DiagnosisReport
from app.services.read_models import effective_run_status


def _snapshot() -> AIOpsSessionSnapshot:
    return AIOpsSessionSnapshot(
        session_id="session-read-model",
        incident_id="inc-read-model",
        trace_id="trace-read-model",
        status="running",
    )


def _approved_request() -> ApprovalRequest:
    return ApprovalRequest(
        incident_id="inc-read-model",
        action="人工调整 Redis maxclients",
        risk_level="high",
        status="approved",
    )


def _report(status: str) -> DiagnosisReport:
    return DiagnosisReport(
        incident_id="inc-read-model",
        trace_id="trace-read-model",
        title="order-service AIOps 诊断报告",
        service_name="order-service",
        severity="P1",
        environment="prod",
        status=status,
        summary="安全变更状态更新",
        markdown="# report",
    )


def test_effective_run_status_preserves_post_approval_change_states() -> None:
    approvals = [_approved_request()]

    assert (
        effective_run_status(_snapshot(), _report("waiting_manual_execution"), approvals)
        == "waiting_manual_execution"
    )
    assert effective_run_status(_snapshot(), _report("sandbox_executing"), approvals) == (
        "sandbox_executing"
    )
    assert effective_run_status(_snapshot(), _report("escalated"), approvals) == "escalated"
