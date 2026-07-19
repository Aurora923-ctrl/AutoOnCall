"""Tests for durable AIOps read-model status resolution."""

from datetime import UTC, datetime

from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.approval import ApprovalRequest
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.read_models import (
    build_aiops_run_status,
    build_aiops_run_summary,
    effective_run_status,
    latest_trace_event,
    list_run_trace_events,
    select_run_approvals,
    select_run_report,
)
from app.services.trace_service import TraceService


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


def test_aiops_run_summary_counts_consumed_plan_steps() -> None:
    snapshot = AIOpsSessionSnapshot.from_state(
        session_id="session-consumed-plan",
        status="running",
        state={
            "trace_id": "trace-consumed-plan",
            "incident": {
                "incident_id": "inc-consumed-plan",
                "title": "Redis timeout",
            },
            "current_plan": [
                {
                    "step_id": "step-2",
                    "tool_name": "search_runbook",
                    "purpose": "查找 Redis 连接耗尽处置手册",
                }
            ],
            "executed_steps": [
                {
                    "step_id": "step-1",
                    "tool_name": "query_redis_status",
                    "purpose": "检查 Redis 连接数",
                    "status": "success",
                }
            ],
            "past_steps": [
                ({"step_id": "step-1", "tool_name": "query_redis_status"}, "ok"),
            ],
        },
    )

    summary = build_aiops_run_summary(snapshot, approvals=[], report=None)

    assert summary["plan_step_count"] == 2
    assert summary["completed_step_count"] == 1


def test_list_run_trace_events_does_not_fallback_to_other_incident_runs(tmp_path) -> None:
    trace_store = TraceService(tmp_path / "run-isolation.db")
    trace_store.create_event(
        trace_id="trace-other",
        incident_id="inc-read-model",
        node_name="planner",
    )
    snapshot = AIOpsSessionSnapshot(
        session_id="session-missing-trace",
        incident_id="inc-read-model",
        trace_id="trace-missing",
        status="running",
    )

    assert list_run_trace_events(snapshot, trace_store) == []


def test_aiops_run_status_redacts_snapshot_and_legacy_report_payloads() -> None:
    snapshot = _snapshot().model_copy(
        update={
            "input": "authorization=Bearer run-secret",
            "incident": {"symptom": "token=incident-secret"},
            "risk_assessment": {"password": "risk-secret"},
            "pending_approval": {"metadata": {"api_key": "approval-secret"}},
            "report": {"summary": "cookie=report-secret"},
            "progress_events": [{"message": "dsn=progress-secret"}],
        }
    )

    payload = build_aiops_run_status(snapshot, events=[], approvals=[], report=None)
    serialized = str(payload)

    for secret in (
        "run-secret",
        "incident-secret",
        "risk-secret",
        "approval-secret",
        "report-secret",
        "progress-secret",
    ):
        assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_run_artifacts_match_trace_when_legacy_approval_has_no_session_id() -> None:
    snapshot = _snapshot()
    matching = ApprovalRequest(
        incident_id=snapshot.incident_id,
        action="matching approval",
        risk_level="high",
        metadata={"trace_id": snapshot.trace_id},
    )
    other = ApprovalRequest(
        incident_id=snapshot.incident_id,
        action="other approval",
        risk_level="high",
        metadata={"trace_id": "trace-other"},
    )

    assert select_run_approvals(snapshot, [other, matching]) == [matching]


def test_run_artifacts_accept_legacy_approval_linked_by_pending_id() -> None:
    snapshot = _snapshot().model_copy(update={"pending_approval": {"approval_id": "apr-legacy"}})
    linked = ApprovalRequest(
        approval_id="apr-legacy",
        incident_id=snapshot.incident_id,
        action="legacy approval",
        risk_level="high",
    )

    assert select_run_approvals(snapshot, [linked]) == [linked]


def test_run_report_rejects_latest_report_from_another_trace() -> None:
    snapshot = _snapshot()
    other_report = _report("completed").model_copy(update={"trace_id": "trace-other"})

    assert select_run_report(snapshot, other_report) is None


def test_latest_trace_event_has_deterministic_tie_breaker() -> None:
    created_at = datetime(2026, 7, 17, tzinfo=UTC)
    older_id = TraceEvent(
        event_id="traceevt-a",
        trace_id="trace-read-model",
        incident_id="inc-read-model",
        node_name="planner",
        created_at=created_at,
    )
    newer_id = older_id.model_copy(update={"event_id": "traceevt-b"})

    assert latest_trace_event([newer_id, older_id]).event_id == "traceevt-b"
