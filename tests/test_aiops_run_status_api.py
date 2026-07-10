"""Tests for durable AIOps diagnosis run status recovery."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api import aiops
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.approval import ApprovalRequest
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent


class FakeAIOpsService:
    def __init__(
        self,
        snapshot: AIOpsSessionSnapshot | None,
        snapshots: list[AIOpsSessionSnapshot] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.snapshots = snapshots if snapshots is not None else ([snapshot] if snapshot else [])

    def get_session_snapshot(self, session_id: str) -> AIOpsSessionSnapshot | None:
        if self.snapshot and self.snapshot.session_id == session_id:
            return self.snapshot
        return None

    def list_session_snapshots(
        self,
        *,
        incident_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[AIOpsSessionSnapshot]:
        snapshots = [
            snapshot
            for snapshot in self.snapshots
            if incident_id is None or snapshot.incident_id == incident_id
        ]
        return snapshots[offset : offset + limit]


class FakeTraceService:
    def __init__(self, events: list[TraceEvent]) -> None:
        self.events = events

    def list_events(
        self,
        *,
        incident_id: str | None = None,
        trace_id: str | None = None,
        event_type: str | None = None,
    ) -> list[TraceEvent]:
        return [
            event
            for event in self.events
            if (incident_id is None or event.incident_id == incident_id)
            and (trace_id is None or event.trace_id == trace_id)
            and (event_type is None or event.event_type == event_type)
        ]


class FakeReportGenerator:
    def __init__(self, report: DiagnosisReport | None) -> None:
        self.report = report

    def get_report(self, incident_id: str) -> DiagnosisReport | None:
        if self.report and self.report.incident_id == incident_id:
            return self.report
        return None


class FakeApprovalService:
    def __init__(self, approvals: list[ApprovalRequest]) -> None:
        self.approvals = approvals

    def list_requests(
        self,
        incident_id: str | None = None,
        status: str | None = None,
    ) -> list[ApprovalRequest]:
        return [
            approval
            for approval in self.approvals
            if (incident_id is None or approval.incident_id == incident_id)
            and (status is None or approval.status == status)
        ]


def _build_test_app(monkeypatch: pytest.MonkeyPatch, services: dict[str, Any]) -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(aiops.router, prefix="/api")
    monkeypatch.setattr(aiops, "aiops_service", services["aiops"])
    monkeypatch.setattr(aiops, "get_trace_service", lambda: services["traces"])
    monkeypatch.setattr(aiops, "get_report_generator", lambda: services["reports"])
    monkeypatch.setattr(aiops, "get_approval_service", lambda: services["approvals"])
    return test_app


@pytest.mark.asyncio
async def test_aiops_run_status_assembles_recovery_payload(monkeypatch) -> None:
    incident_id = "INC-RECOVER-001"
    trace_id = "trace-recover"
    snapshot = AIOpsSessionSnapshot.from_state(
        session_id="run-recover",
        status="running",
        node_name="executor",
        state={
            "input": "诊断 order-service Redis maxclients",
            "trace_id": trace_id,
            "incident": {
                "incident_id": incident_id,
                "title": "order-service Redis maxclients exhausted",
                "service_name": "order-service",
                "severity": "P1",
                "environment": "prod",
                "symptom": "Redis connection timeout",
            },
            "current_plan": [
                {
                    "step_id": "step-1",
                    "tool_name": "query_redis_status",
                    "purpose": "检查 Redis 连接数",
                }
            ],
            "past_steps": [
                (
                    {"step_id": "step-1", "tool_name": "query_redis_status"},
                    {"output_summary": "connected_clients=9940/10000", "status": "success"},
                )
            ],
            "tool_call_records": [
                {
                    "step_id": "step-1",
                    "tool_name": "query_redis_status",
                    "status": "success",
                    "output_summary": "connected_clients=9940/10000",
                }
            ],
            "warnings": ["步骤 s9 使用了 LLM ToolNode 兜底路径，结果需用标准工具复核。"],
            "gathered_evidence": [
                {
                    "step_id": "step-1",
                    "source_tool": "query_redis_status",
                    "summary": "Redis connected_clients 接近 maxclients",
                }
            ],
            "progress": {
                "phase": "executing",
                "node_name": "executor",
                "current_tool": "query_redis_status",
                "tool_total": 1,
                "tool_success_count": 1,
                "tool_failed_count": 0,
                "evidence_count": 1,
                "risk_policy": "allow",
                "report_status": "not_started",
                "cursor": "run-recover:000003",
                "status": "running",
            },
            "progress_cursor": "run-recover:000003",
            "progress_events": [
                {
                    "cursor": "run-recover:000003",
                    "phase": "executing",
                    "node_name": "executor",
                }
            ],
        },
    )
    report = DiagnosisReport(
        report_id="rpt-recover",
        incident_id=incident_id,
        trace_id=trace_id,
        status="waiting_approval",
        title="order-service AIOps 诊断报告",
        service_name="order-service",
        severity="P1",
        environment="prod",
        markdown="# order-service AIOps 诊断报告",
    )
    trace_event = TraceEvent(
        trace_id=trace_id,
        incident_id=incident_id,
        node_name="executor",
        event_type="tool_call",
        status="success",
        output_summary="Redis connected_clients 接近 maxclients",
    )
    approval = ApprovalRequest(
        incident_id=incident_id,
        action="调整 Redis maxclients 配置",
        risk_level="high",
        reason="生产配置变更需要审批",
        metadata={"trace_id": trace_id, "session_id": "run-recover"},
    )
    test_app = _build_test_app(
        monkeypatch,
        {
            "aiops": FakeAIOpsService(snapshot),
            "traces": FakeTraceService([trace_event]),
            "reports": FakeReportGenerator(report),
            "approvals": FakeApprovalService([approval]),
        },
    )

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/aiops/runs/run-recover")

    assert response.status_code == 200
    payload = response.json()
    assert payload["diagnosis_run_id"] == "run-recover"
    assert payload["session_id"] == "run-recover"
    assert payload["incident_id"] == incident_id
    assert payload["trace_id"] == trace_id
    assert payload["status"] == "waiting_approval"
    assert payload["status_metadata"]["phase"] == "approval"
    assert payload["status_metadata"]["tone"] == "warning"
    assert payload["node_name"] == "executor"
    assert payload["has_report"] is True
    assert payload["report_id"] == "rpt-recover"
    assert payload["current_plan"][0]["tool_name"] == "query_redis_status"
    assert payload["tool_call_records"][0]["status"] == "success"
    assert payload["warnings"][0].startswith("步骤 s9 使用了 LLM ToolNode")
    assert payload["gathered_evidence"][0]["source_tool"] == "query_redis_status"
    assert payload["progress"]["phase"] == "executing"
    assert payload["progress"]["current_tool"] == "query_redis_status"
    assert payload["progress"]["tool_success_count"] == 1
    assert payload["progress_cursor"] == "run-recover:000003"
    assert payload["progress_events"][0]["cursor"] == "run-recover:000003"
    assert payload["trace_summary"]["event_count"] == 1
    assert payload["trace_summary"]["latest_event_type"] == "tool_call"
    assert payload["approval_summary"]["status"] == "pending"
    assert payload["approval_summary"]["by_status"]["pending"] == 1
    assert payload["links"]["run"] == "/api/aiops/runs/run-recover"
    assert payload["links"]["report"] == f"/api/incidents/{incident_id}/report"


@pytest.mark.asyncio
async def test_aiops_run_status_uses_effective_approval_decision(monkeypatch) -> None:
    incident_id = "INC-APPROVAL-REJECTED-001"
    trace_id = "trace-approval-rejected"
    snapshot = AIOpsSessionSnapshot.from_state(
        session_id="run-approval-rejected",
        status="waiting_approval",
        node_name="replanner",
        state={
            "input": "诊断 order-service 高风险变更",
            "trace_id": trace_id,
            "incident": {
                "incident_id": incident_id,
                "title": "order-service risky remediation",
                "service_name": "order-service",
                "severity": "P1",
                "environment": "prod",
                "symptom": "需要执行高风险修复动作",
            },
            "pending_approval": {"approval_id": "apr-rejected", "status": "pending"},
        },
    )
    report = DiagnosisReport(
        report_id="rpt-approval-rejected",
        incident_id=incident_id,
        trace_id=trace_id,
        status="approval_rejected",
        title="order-service AIOps 诊断报告",
        service_name="order-service",
        severity="P1",
        environment="prod",
        markdown="# order-service AIOps 诊断报告",
    )
    approval = ApprovalRequest(
        approval_id="apr-rejected",
        incident_id=incident_id,
        action="执行高风险修复动作",
        risk_level="high",
        reason="生产高风险动作需要审批",
        status="rejected",
        decided_by="operator",
        decision_reason="风险过高，先观察",
        metadata={"trace_id": trace_id, "session_id": "run-approval-rejected"},
    )
    test_app = _build_test_app(
        monkeypatch,
        {
            "aiops": FakeAIOpsService(snapshot),
            "traces": FakeTraceService([]),
            "reports": FakeReportGenerator(report),
            "approvals": FakeApprovalService([approval]),
        },
    )

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        detail_response = await client.get("/api/aiops/runs/run-approval-rejected")
        list_response = await client.get("/api/aiops/runs?status=approval_rejected")

    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["status"] == "approval_rejected"
    assert detail_payload["approval_summary"]["status"] == "rejected"

    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["count"] == 1
    assert list_payload["items"][0]["session_id"] == "run-approval-rejected"
    assert list_payload["items"][0]["status"] == "approval_rejected"
    assert list_payload["items"][0]["approval_status"] == "rejected"
    assert list_payload["items"][0]["has_pending_approval"] is False


@pytest.mark.asyncio
async def test_aiops_run_list_returns_compact_history(monkeypatch) -> None:
    incident_id = "INC-RECOVER-001"
    snapshot = AIOpsSessionSnapshot.from_state(
        session_id="run-history",
        status="completed",
        node_name="workflow",
        state={
            "input": "诊断 order-service Redis maxclients",
            "trace_id": "trace-history",
            "incident": {
                "incident_id": incident_id,
                "title": "order-service Redis maxclients exhausted",
                "service_name": "order-service",
                "severity": "P1",
                "environment": "prod",
                "symptom": "Redis connection timeout",
            },
            "plan": [
                {"step_id": "step-1", "tool_name": "query_redis_status"},
                {"step_id": "step-2", "tool_name": "search_runbook"},
            ],
            "past_steps": [
                ({"step_id": "step-1"}, "connected_clients=9940/10000"),
            ],
            "tool_call_records": [{"tool_name": "query_redis_status"}],
            "gathered_evidence": [{"summary": "Redis connected_clients 接近 maxclients"}],
            "warnings": ["使用 LLM ToolNode 兜底路径"],
            "report": {"report_id": "rpt-history", "status": "completed"},
        },
    )
    test_app = _build_test_app(
        monkeypatch,
        {
            "aiops": FakeAIOpsService(snapshot),
            "traces": FakeTraceService([]),
            "reports": FakeReportGenerator(None),
            "approvals": FakeApprovalService([]),
        },
    )

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/api/aiops/runs?incident_id={incident_id}&limit=10")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["session_id"] == "run-history"
    assert item["incident_id"] == incident_id
    assert item["status"] == "completed"
    assert item["status_metadata"]["tone"] == "success"
    assert item["title"] == "order-service Redis maxclients exhausted"
    assert item["plan_step_count"] == 2
    assert item["completed_step_count"] == 1
    assert item["tool_call_count"] == 1
    assert item["evidence_count"] == 1
    assert item["warning_count"] == 1
    assert item["has_report"] is True
    assert item["report_id"] == "rpt-history"
    assert item["links"]["run"] == "/api/aiops/runs/run-history"


@pytest.mark.asyncio
async def test_aiops_run_list_filters_by_status_and_service(monkeypatch) -> None:
    order_snapshot = AIOpsSessionSnapshot.from_state(
        session_id="run-order-completed",
        status="completed",
        node_name="workflow",
        state={
            "trace_id": "trace-order",
            "incident": {
                "incident_id": "INC-ORDER-001",
                "title": "order-service Redis maxclients exhausted",
                "service_name": "order-service",
                "severity": "P1",
                "environment": "prod",
            },
        },
    )
    payment_snapshot = AIOpsSessionSnapshot.from_state(
        session_id="run-payment-running",
        status="running",
        node_name="executor",
        state={
            "trace_id": "trace-payment",
            "incident": {
                "incident_id": "INC-PAYMENT-001",
                "title": "payment-service MySQL slow query",
                "service_name": "payment-service",
                "severity": "P2",
                "environment": "prod",
            },
        },
    )
    test_app = _build_test_app(
        monkeypatch,
        {
            "aiops": FakeAIOpsService(
                order_snapshot,
                snapshots=[order_snapshot, payment_snapshot],
            ),
            "traces": FakeTraceService([]),
            "reports": FakeReportGenerator(None),
            "approvals": FakeApprovalService([]),
        },
    )

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/aiops/runs?status=completed&service_name=order")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["filters"]["status"] == "completed"
    assert payload["filters"]["service_name"] == "order"
    assert payload["items"][0]["session_id"] == "run-order-completed"
    assert payload["items"][0]["service_name"] == "order-service"


@pytest.mark.asyncio
async def test_aiops_run_list_filter_scans_beyond_first_page(monkeypatch) -> None:
    non_matching = [
        AIOpsSessionSnapshot.from_state(
            session_id=f"run-payment-{index}",
            status="running",
            node_name="workflow",
            state={
                "trace_id": f"trace-payment-{index}",
                "incident": {
                    "incident_id": f"INC-PAYMENT-{index:03d}",
                    "title": "payment-service running diagnosis",
                    "service_name": "payment-service",
                    "severity": "P2",
                    "environment": "prod",
                },
            },
        )
        for index in range(101)
    ]
    matching = AIOpsSessionSnapshot.from_state(
        session_id="run-order-completed-late",
        status="completed",
        node_name="workflow",
        state={
            "trace_id": "trace-order-late",
            "incident": {
                "incident_id": "INC-ORDER-LATE",
                "title": "order-service completed diagnosis",
                "service_name": "order-service",
                "severity": "P1",
                "environment": "prod",
            },
        },
    )
    test_app = _build_test_app(
        monkeypatch,
        {
            "aiops": FakeAIOpsService(
                matching,
                snapshots=[*non_matching, matching],
            ),
            "traces": FakeTraceService([]),
            "reports": FakeReportGenerator(None),
            "approvals": FakeApprovalService([]),
        },
    )

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/aiops/runs?status=completed&service_name=order")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["session_id"] == "run-order-completed-late"


@pytest.mark.asyncio
async def test_aiops_run_status_returns_404_for_unknown_session(monkeypatch) -> None:
    test_app = _build_test_app(
        monkeypatch,
        {
            "aiops": FakeAIOpsService(None),
            "traces": FakeTraceService([]),
            "reports": FakeReportGenerator(None),
            "approvals": FakeApprovalService([]),
        },
    )

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/aiops/runs/missing-run")

    assert response.status_code == 404
    assert response.json()["detail"] == "AIOps diagnosis run not found"


@pytest.mark.asyncio
async def test_aiops_run_status_derives_progress_for_legacy_snapshot(monkeypatch) -> None:
    snapshot = AIOpsSessionSnapshot.from_state(
        session_id="run-legacy-progress",
        status="running",
        node_name="executor",
        state={
            "trace_id": "trace-legacy-progress",
            "incident": {
                "incident_id": "INC-LEGACY-PROGRESS",
                "service_name": "order-service",
            },
            "current_plan": [{"step_id": "s2", "tool_name": "query_logs", "status": "pending"}],
            "past_steps": [
                ({"step_id": "s1", "tool_name": "query_metrics"}, "ok"),
            ],
            "tool_call_records": [
                {"tool_name": "query_metrics", "status": "success"},
            ],
            "gathered_evidence": [{"summary": "metrics ok"}],
        },
    )
    test_app = _build_test_app(
        monkeypatch,
        {
            "aiops": FakeAIOpsService(snapshot),
            "traces": FakeTraceService([]),
            "reports": FakeReportGenerator(None),
            "approvals": FakeApprovalService([]),
        },
    )

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/aiops/runs/run-legacy-progress")

    assert response.status_code == 200
    payload = response.json()
    progress = payload["progress"]
    assert progress["phase"] == "executing"
    assert progress["node_name"] == "executor"
    assert progress["current_tool"] == "query_logs"
    assert progress["tool_total"] == 2
    assert progress["tool_success_count"] == 1
    assert progress["tool_failed_count"] == 0
    assert progress["evidence_count"] == 1
    assert progress["risk_policy"] == "allow"
    assert progress["report_status"] == "not_started"
    assert payload["progress_cursor"] == "run-legacy-progress:snapshot"
