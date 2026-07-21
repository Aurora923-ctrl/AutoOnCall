"""Focused tests for report construction and lifecycle components."""

from app.agent.aiops import create_initial_aiops_state
from app.models.report import DiagnosisReport
from app.services.report_builder import ReportBuilder
from app.services.report_lifecycle import ReportLifecycle


def test_report_builder_builds_without_persisting() -> None:
    state = create_initial_aiops_state(
        "checkout-service timeout",
        session_id="report-builder-test",
    )
    state["incident"]["service_name"] = "checkout-service"

    report = ReportBuilder().build_from_state(
        state,
        trace_events=[],
        status="needs_human",
    )

    assert report.incident_id == state["incident"]["incident_id"]
    assert report.service_name == "checkout-service"
    assert report.status == "needs_human"
    assert report.markdown == ""


def test_report_lifecycle_applies_approval_decision_and_renders_markdown() -> None:
    report = DiagnosisReport(
        incident_id="inc-report-lifecycle",
        status="waiting_approval",
        approval_status="pending",
        manual_action_required=True,
        approval_decision={
            "approval_id": "apr-report-lifecycle",
            "action": "restart production pod",
            "status": "pending",
        },
        summary="Awaiting approval.",
    )

    updated = ReportLifecycle().apply_approval_decision(
        report,
        approval_status="rejected",
        decided_by="sre",
        reason="rollback plan missing",
    )

    assert updated.status == "approval_rejected"
    assert updated.approval_status == "rejected"
    assert updated.approval_decision["decided_by"] == "sre"
    assert updated.approval_decision["decision_reason"] == "rollback plan missing"
    assert updated.markdown


def test_report_lifecycle_upserts_change_execution_snapshot() -> None:
    report = DiagnosisReport(
        incident_id="inc-change-lifecycle",
        status="approval_approved",
        approval_status="approved",
        manual_action_required=True,
        summary="Approved.",
    )
    lifecycle = ReportLifecycle()
    first = lifecycle.apply_change_execution(
        report,
        execution={
            "change_execution_id": "chg-1",
            "approval_id": "apr-1",
            "status": "waiting_manual_execution",
        },
    )
    updated = lifecycle.apply_change_execution(
        first,
        execution={
            "change_execution_id": "chg-1",
            "approval_id": "apr-1",
            "status": "dry_run_completed",
        },
    )

    assert updated.status == "change_validated"
    assert updated.manual_action_required is False
    assert len(updated.change_executions) == 1
    assert updated.change_executions[0]["status"] == "dry_run_completed"
    assert updated.markdown
