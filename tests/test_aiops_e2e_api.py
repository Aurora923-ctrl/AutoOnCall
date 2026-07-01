"""End-to-end HTTP tests for the AIOps diagnosis and incident query loop."""

import importlib
import json
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import aiops, approvals, incidents
from app.models.approval import ApprovalRequest
from app.services.aiops_service import AIOpsService
from app.services.approval_service import ApprovalService
from app.services.report_generator import ReportGenerator
from app.services.trace_service import TraceService


def _parse_sse_events(text: str) -> list[dict]:
    events = []
    data_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                events.append(json.loads("\n".join(data_lines)))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    if data_lines:
        events.append(json.loads("\n".join(data_lines)))
    return events


@pytest.mark.asyncio
async def test_demo_incident_run_delegates_to_standard_aiops_stream(monkeypatch) -> None:
    class FakeDemoAIOpsService:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def diagnose(self, session_id: str, incident):
            self.calls.append({"session_id": session_id, "incident": incident})
            yield {
                "type": "complete",
                "stage": "diagnosis_complete",
                "message": "demo complete",
                "incident_id": incident.incident_id,
                "status": "completed",
            }

    fake_service = FakeDemoAIOpsService()
    monkeypatch.setattr(aiops, "aiops_service", fake_service)
    test_app = FastAPI()
    test_app.include_router(aiops.router, prefix="/api")

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/aiops/demo/incidents/redis-maxclients/run", json={})

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert events[-1]["type"] == "complete"
    assert fake_service.calls[0]["session_id"] == "demo-redis_maxclients"
    assert fake_service.calls[0]["incident"].incident_id == "INC-REDIS-001"


@pytest.mark.asyncio
async def test_aiops_sse_incident_trace_report_and_approval_e2e(monkeypatch, tmp_path) -> None:
    test_app = FastAPI()
    test_app.include_router(aiops.router, prefix="/api")
    test_app.include_router(approvals.router, prefix="/api")
    test_app.include_router(incidents.router, prefix="/api")

    trace_store = TraceService(tmp_path / "trace.db")
    report_store = ReportGenerator(tmp_path / "report.db")
    approval_store = ApprovalService(tmp_path / "approval.db")

    approvals_api = importlib.import_module("app.api.approvals")
    incidents_api = importlib.import_module("app.api.incidents")
    aiops_api = importlib.import_module("app.api.aiops")
    aiops_service_module = importlib.import_module("app.services.aiops_service")
    approval_service_module = importlib.import_module("app.services.approval_service")
    report_generator_module = importlib.import_module("app.services.report_generator")

    async def fake_planner(state: dict) -> dict:
        return {
            "current_plan": [
                {
                    "step_id": "s1",
                    "tool_name": "query_metrics",
                    "purpose": "检查 order-service 指标",
                    "input_args": {"service_name": "order-service"},
                    "expected_evidence": "延迟和错误率证据",
                    "risk_level": "low",
                    "status": "pending",
                }
            ],
            "plan": ["[s1] 使用 query_metrics: 检查 order-service 指标"],
        }

    async def fake_executor(state: dict) -> dict:
        evidence = {
            "evidence_id": "ev-e2e",
            "incident_id": state["incident"]["incident_id"],
            "trace_id": state["trace_id"],
            "source_tool": "query_metrics",
            "step_id": "s1",
            "summary": "P95 延迟升高且 5xx 错误率升高",
            "raw_data": {"source": "mock", "p95_latency_ms": {"current": 3200}},
            "confidence": 0.9,
        }
        return {
            "current_plan": [],
            "plan": [],
            "past_steps": [("检查 order-service 指标", "P95=3200ms, 5xx=8.2%")],
            "gathered_evidence": [evidence],
            "tool_call_records": [
                {
                    "trace_id": state["trace_id"],
                    "incident_id": state["incident"]["incident_id"],
                    "step_id": "s1",
                    "tool_name": "query_metrics",
                    "input_args": {"service_name": "order-service"},
                    "output": {"summary": evidence["summary"]},
                    "latency_ms": 1.2,
                    "status": "success",
                }
            ],
        }

    async def fake_replanner(state: dict) -> dict:
        approval = approval_store.create_request(
            ApprovalRequest(
                incident_id=state["incident"]["incident_id"],
                action="调整 Redis maxclients 配置",
                risk_level="medium",
                reason="生产配置变更需要审批",
                metadata={"trace_id": state["trace_id"]},
            )
        )
        report_state = dict(state)
        report_state["response"] = "# order-service AIOps 诊断报告"
        report_state["pending_approval"] = approval.model_dump(mode="json")
        report = report_store.generate_from_state(report_state, status="waiting_approval")
        return {
            "response": report.markdown,
            "pending_approval": approval.model_dump(mode="json"),
            "report": report.model_dump(mode="json"),
            "hypotheses": ["Redis 连接数接近上限"],
            "final_diagnosis": report.root_cause,
        }

    monkeypatch.setattr(aiops_service_module, "planner", fake_planner)
    monkeypatch.setattr(aiops_service_module, "executor", fake_executor)
    monkeypatch.setattr(aiops_service_module, "replanner", fake_replanner)
    monkeypatch.setattr(aiops_service_module, "trace_service", trace_store)
    monkeypatch.setattr(approval_service_module, "trace_service", trace_store)
    monkeypatch.setattr(report_generator_module, "trace_service", trace_store)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: trace_store)
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: report_store)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approval_store)
    monkeypatch.setattr(approvals_api, "get_approval_service", lambda: approval_store)
    monkeypatch.setattr(aiops_api, "aiops_service", AIOpsService())

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/aiops",
            json={
                "session_id": f"e2e-{uuid4().hex}",
                "incident": {
                    "title": "order-service Redis timeout",
                    "service_name": "order-service",
                    "severity": "P1",
                    "symptom": "5xx 错误率升高，P95 延迟超过 3 秒，并出现 Redis connection timeout",
                    "environment": "prod",
                },
            },
            timeout=30,
        )

        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        assert {event["type"] for event in events} >= {"plan", "step_complete", "complete"}

        complete_event = events[-1]
        incident_id = complete_event["incident_id"]
        assert incident_id.startswith("inc-")
        assert complete_event["trace_id"].startswith("trace-")
        assert complete_event["structured_report"]
        assert complete_event["status"] == complete_event["structured_report"]["status"]
        assert (
            complete_event["diagnosis"]["status"] == complete_event["structured_report"]["status"]
        )

        overview = await client.get(f"/api/incidents/{incident_id}")
        trace = await client.get(f"/api/incidents/{incident_id}/trace")
        report = await client.get(f"/api/incidents/{incident_id}/report")
        approval_response = await client.get(f"/api/incidents/{incident_id}/approval")

        assert overview.status_code == 200
        assert trace.status_code == 200
        assert report.status_code == 200
        assert approval_response.status_code == 200
        assert overview.json()["trace_summary"]["event_count"] >= 1
        assert report.json()["report"]["incident_id"] == incident_id
        assert approval_response.json()["incident_id"] == incident_id
