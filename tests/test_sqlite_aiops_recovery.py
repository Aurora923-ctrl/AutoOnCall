"""SQLite recovery tests across AIOps trace, approval, and report services."""

import importlib

import pytest

from app.models.approval import ApprovalRequest
from app.models.report import DiagnosisReport
from app.services.approval_service import ApprovalService
from app.services.report_generator import ReportGenerator
from app.services.trace_service import TraceService


@pytest.mark.asyncio
async def test_sqlite_recovers_incident_state_across_aiops_services(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "aiops-state.db"
    incident_id = "inc-recovery"
    trace_id = "trace-recovery"

    traces = TraceService(database_path)
    approvals = ApprovalService(database_path)
    reports = ReportGenerator(database_path)

    traces.create_event(
        trace_id=trace_id,
        incident_id=incident_id,
        node_name="workflow",
        event_type="workflow_started",
        output_summary="workflow started",
    )
    approvals.create_request(
        ApprovalRequest(
            incident_id=incident_id,
            action="调整 Redis maxclients 配置",
            risk_level="medium",
            reason="生产配置变更需要审批",
            metadata={"trace_id": trace_id},
        )
    )
    reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            title="order-service AIOps 诊断报告",
            service_name="order-service",
            severity="P1",
            environment="prod",
            status="waiting_approval",
            summary="Redis 连接数接近上限",
            root_cause="Redis maxclients 接近上限",
            manual_action_required=True,
            approval_status="pending",
            markdown="# order-service AIOps 诊断报告",
            confidence=0.82,
        )
    )

    reloaded_traces = TraceService(database_path)
    reloaded_approvals = ApprovalService(database_path)
    reloaded_reports = ReportGenerator(database_path)

    incidents_api = importlib.import_module("app.api.incidents")
    approvals_api = importlib.import_module("app.api.approvals")
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: reloaded_traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: reloaded_approvals)
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reloaded_reports)
    monkeypatch.setattr(approvals_api, "get_approval_service", lambda: reloaded_approvals)

    overview = await incidents_api.get_incident_overview(incident_id)
    trace = await incidents_api.get_incident_trace(incident_id)
    report = await incidents_api.get_incident_report(incident_id)
    approval = await approvals_api.list_incident_approvals(incident_id, status=None)

    assert overview["incident_id"] == incident_id
    assert overview["trace_summary"]["event_count"] >= 1
    assert overview["approval_summary"]["by_status"]["pending"] == 1
    assert overview["report"]["incident_id"] == incident_id
    assert trace["items"][0]["incident_id"] == incident_id
    assert report["report"]["incident_id"] == incident_id
    assert approval["items"][0]["incident_id"] == incident_id
    assert not list(tmp_path.glob("*.jsonl"))
