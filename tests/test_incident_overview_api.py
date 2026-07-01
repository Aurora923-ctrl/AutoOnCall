"""Tests for incident overview APIs used by the AIOps demo loop."""

import importlib

import pytest
from fastapi import HTTPException

from app.models.approval import ApprovalRequest
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.services.approval_service import ApprovalService
from app.services.report_generator import ReportGenerator
from app.services.sqlite_store import AIOpsSQLiteStore
from app.services.trace_service import TraceService


@pytest.mark.asyncio
async def test_incident_overview_aggregates_report_trace_and_approvals(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-demo"
    trace_id = "trace-demo"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    report = DiagnosisReport(
        incident_id=incident_id,
        trace_id=trace_id,
        title="order-service AIOps 诊断报告",
        service_name="order-service",
        severity="P1",
        environment="prod",
        status="waiting_approval",
        summary="Redis 连接数接近上限",
        root_cause="Redis maxclients 接近上限",
        evidence=[
            {
                "step_id": "s1",
                "source_tool": "query_redis_status",
                "data_source": "mock",
                "evidence_type": "redis",
                "stance": "supporting",
                "summary": "connected_clients=9940/10000",
                "fact": "Redis connected_clients=9940/10000；来源=mock",
                "inference": "该证据支持当前根因假设。",
                "uncertainty": "该证据来自 Mock 回退。",
                "next_step": "接入真实适配器后重复该步骤。",
                "confidence": 0.75,
            },
            {
                "step_id": "s2",
                "source_tool": "query_traces",
                "data_source": "jaeger",
                "evidence_type": "trace",
                "stance": "supporting",
                "summary": "Jaeger 返回 2 条 trace，error_spans=1",
                "confidence_reason": "Tracing 后端返回调用链耗时和错误 span 信号",
                "confidence": 0.82,
            }
        ],
        tool_calls=[
            {
                "step_id": "s1",
                "tool_name": "query_redis_status",
                "data_source": "mock",
                "status": "success",
                "latency_ms": 12.5,
                "input_summary": '{"service_name": "order-service"}',
                "output_summary": "connected_clients=9940/10000",
            },
            {
                "step_id": "s2",
                "tool_name": "query_traces",
                "data_source": "jaeger",
                "status": "success",
                "latency_ms": 18.5,
                "input_summary": '{"service_name": "order-service"}',
                "output_summary": "Jaeger 返回 2 条 trace，error_spans=1",
            }
        ],
        confirmed_facts=["Redis connected_clients=9940/10000；来源=mock"],
        inferred_conclusions=["该证据支持当前根因假设。"],
        uncertainties=["该证据来自 Mock 回退。"],
        next_steps=["接入真实适配器后重复该步骤。"],
        manual_action_required=True,
        approval_status="pending",
        trace_summary={"event_count": 1},
        markdown="# order-service AIOps 诊断报告",
        confidence=0.82,
    )
    reports.save_report(report)
    traces.create_event(
        trace_id=trace_id,
        incident_id=incident_id,
        node_name="executor",
        event_type="tool_call",
        status="success",
        output_summary="Redis 连接数接近上限",
    )
    approvals.create_request(
        ApprovalRequest(
            incident_id=incident_id,
            action="调整 Redis maxclients 配置",
            risk_level="high",
            reason="生产配置变更需要审批",
            metadata={"trace_id": trace_id},
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)

    overview = await incidents_api.get_incident_overview(incident_id)
    listing = await incidents_api.list_incidents()

    assert overview["incident_id"] == incident_id
    assert overview["trace_id"] == trace_id
    assert overview["status"] == "waiting_approval"
    assert overview["status_metadata"]["phase"] == "approval"
    assert overview["status_metadata"]["tone"] == "warning"
    assert overview["lifecycle"] is None
    assert overview["trace_summary"]["event_count"] == 1
    assert overview["approval_summary"]["by_status"]["pending"] == 1
    assert overview["diagnosis_chain"]["tool_calls"][0]["data_source"] == "mock"
    assert overview["diagnosis_chain"]["dependency_signals"][0]["backend"] == "jaeger"
    assert overview["diagnosis_chain"]["dependency_signals"][0]["domain"] == "tracing"
    assert overview["diagnosis_chain"]["data_sources"]["has_mock"] is True
    assert overview["diagnosis_chain"]["confirmed_facts"]
    assert overview["diagnosis_chain"]["next_steps"]
    assert overview["links"]["report"] == f"/api/incidents/{incident_id}/report"
    assert listing["items"][0]["incident_id"] == incident_id

    approvals.decide_latest_pending(
        incident_id=incident_id,
        decision="approve",
        decided_by="pytest",
        reason="verified manual mitigation",
    )
    approved_overview = await incidents_api.get_incident_overview(incident_id)

    assert approved_overview["status"] == "approval_approved"
    assert approved_overview["approval_status"] == "approved"
    assert approved_overview["manual_action_required"] is True
    assert approved_overview["approval_summary"]["by_status"]["approved"] == 1


@pytest.mark.asyncio
async def test_incident_overview_prefers_durable_lifecycle_state(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-lifecycle"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id="trace-lifecycle",
            title="order-service AIOps 诊断报告",
            service_name="order-service",
            severity="P1",
            environment="prod",
            status="approval_approved",
            summary="审批已通过",
            markdown="# report",
        )
    )
    states.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            status="change_dry_run",
            status_reason="Safe change workflow status=dry_run_running",
            title="order-service AIOps 诊断报告",
            service_name="order-service",
            severity="P1",
            environment="prod",
            trace_id="trace-lifecycle",
            session_id="session-lifecycle",
            approval_status="approved",
            manual_action_required=True,
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)

    overview = await incidents_api.get_incident_overview(incident_id)

    assert overview["status"] == "change_dry_run"
    assert overview["status_metadata"]["phase"] == "change"
    assert overview["status_reason"] == "Safe change workflow status=dry_run_running"
    assert overview["session_id"] == "session-lifecycle"
    assert overview["lifecycle"]["status"] == "change_dry_run"


@pytest.mark.asyncio
async def test_incident_overview_returns_404_for_unknown_incident(monkeypatch, tmp_path) -> None:
    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(
        incidents_api,
        "get_report_generator",
        lambda: ReportGenerator(tmp_path / "reports.db"),
    )
    monkeypatch.setattr(
        incidents_api,
        "get_trace_service",
        lambda: TraceService(tmp_path / "traces.db"),
    )
    monkeypatch.setattr(
        incidents_api,
        "get_approval_service",
        lambda: ApprovalService(tmp_path / "approvals.db"),
    )
    monkeypatch.setattr(
        incidents_api,
        "get_incident_state_store",
        lambda: AIOpsSQLiteStore(tmp_path / "states.db"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await incidents_api.get_incident_overview("inc-missing")

    assert exc_info.value.status_code == 404
