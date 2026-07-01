"""Mainline AIOps API tests that keep the real graph nodes in the loop."""

from __future__ import annotations

import importlib
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import aiops, approvals, incidents
from app.config import config
from app.services.aiops_service import AIOpsService
from app.services.approval_service import ApprovalService
from app.services.report_generator import ReportGenerator
from app.services.trace_service import TraceService


class EmptyMCPClient:
    async def get_tools(self) -> list[Any]:
        return []


async def fake_get_mcp_client_with_retry() -> EmptyMCPClient:
    return EmptyMCPClient()


class FailingPlannerLLM:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def with_structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("LLM disabled in mainline API test")


def _raise_disabled_llm() -> Any:
    raise RuntimeError("LLM disabled in mainline API test")


def _parse_sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
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


def _build_test_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(aiops.router, prefix="/api")
    test_app.include_router(approvals.router, prefix="/api")
    test_app.include_router(incidents.router, prefix="/api")

    db_path = tmp_path / "aiops-mainline.db"
    trace_store = TraceService(db_path)
    report_store = ReportGenerator(db_path)
    approval_store = ApprovalService(db_path)

    aiops_api = importlib.import_module("app.api.aiops")
    approvals_api = importlib.import_module("app.api.approvals")
    incidents_api = importlib.import_module("app.api.incidents")
    planner_module = importlib.import_module("app.agent.aiops.planner")
    executor_module = importlib.import_module("app.agent.aiops.executor")
    replanner_module = importlib.import_module("app.agent.aiops.replanner")
    service_module = importlib.import_module("app.services.aiops_service")
    approval_service_module = importlib.import_module("app.services.approval_service")
    report_generator_module = importlib.import_module("app.services.report_generator")

    monkeypatch.setattr(config, "aiops_mock_fallback_enabled", True)
    monkeypatch.setattr(planner_module, "ChatQwen", FailingPlannerLLM)
    monkeypatch.setattr(
        planner_module, "retrieve_structured_knowledge", lambda _: {"status": "empty"}
    )
    monkeypatch.setattr(planner_module, "get_mcp_client_with_retry", fake_get_mcp_client_with_retry)
    monkeypatch.setattr(
        executor_module, "get_mcp_client_with_retry", fake_get_mcp_client_with_retry
    )
    monkeypatch.setattr(replanner_module, "_create_llm", _raise_disabled_llm)

    monkeypatch.setattr(service_module, "trace_service", trace_store)
    monkeypatch.setattr(service_module, "report_generator", report_store)
    monkeypatch.setattr(executor_module, "trace_service", trace_store)
    monkeypatch.setattr(executor_module, "approval_service", approval_store)
    monkeypatch.setattr(replanner_module, "trace_service", trace_store)
    monkeypatch.setattr(replanner_module, "report_generator", report_store)
    monkeypatch.setattr(replanner_module, "approval_service", approval_store)
    monkeypatch.setattr(approval_service_module, "trace_service", trace_store)
    monkeypatch.setattr(report_generator_module, "trace_service", trace_store)

    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: trace_store)
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: report_store)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approval_store)
    monkeypatch.setattr(approvals_api, "get_approval_service", lambda: approval_store)
    monkeypatch.setattr(aiops_api, "get_approval_service", lambda: approval_store)
    monkeypatch.setattr(aiops_api, "aiops_service", AIOpsService())

    return test_app


@pytest.mark.asyncio
async def test_aiops_api_runs_real_graph_nodes_with_fallbacks(monkeypatch, tmp_path) -> None:
    test_app = _build_test_app(monkeypatch, tmp_path)

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/aiops",
            json={
                "session_id": f"mainline-{uuid4().hex}",
                "incident": {
                    "title": "order-service Redis maxclients exhausted",
                    "service_name": "order-service",
                    "severity": "P1",
                    "symptom": "Redis connection timeout，接口 5xx 上升，怀疑 maxclients 耗尽",
                    "environment": "prod",
                },
            },
            timeout=30,
        )

        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        event_types = {event["type"] for event in events}
        assert "plan" in event_types
        assert "step_complete" in event_types
        assert "report" in event_types
        assert events[-1]["type"] == "complete"

        complete_event = events[-1]
        structured_report = complete_event["structured_report"]
        incident_id = complete_event["incident_id"]
        assert complete_event["status"] == structured_report["status"]
        assert complete_event["diagnosis"]["status"] == structured_report["status"]
        assert incident_id == structured_report["incident_id"]
        assert complete_event["trace_id"] == structured_report["trace_id"]

        trace_response = await client.get(f"/api/incidents/{incident_id}/trace")
        report_response = await client.get(f"/api/incidents/{incident_id}/report")
        overview_response = await client.get(f"/api/incidents/{incident_id}")
        approval_response = await client.get(f"/api/incidents/{incident_id}/approval")

        assert trace_response.status_code == 200
        assert report_response.status_code == 200
        assert overview_response.status_code == 200
        assert approval_response.status_code == 200
        assert len(trace_response.json()["items"]) >= 1
        assert report_response.json()["report"]["incident_id"] == incident_id
        assert overview_response.json()["incident_id"] == incident_id
        assert overview_response.json()["trace_summary"]["event_count"] >= 1

        approval_items = approval_response.json()["items"]
        assert approval_items == []


@pytest.mark.asyncio
async def test_aiops_sse_contract_exposes_structured_terminal_status(monkeypatch, tmp_path) -> None:
    test_app = _build_test_app(monkeypatch, tmp_path)

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/aiops",
            json={
                "session_id": f"sse-contract-{uuid4().hex}",
                "incident": {
                    "title": "payment-service MySQL slow query latency",
                    "service_name": "payment-service",
                    "severity": "P2",
                    "symptom": "接口响应慢，日志出现 MySQL 慢查询和连接池等待",
                    "environment": "prod",
                },
            },
            timeout=30,
        )

    events = _parse_sse_events(response.text)
    assert events
    for event in events:
        assert isinstance(event.get("type"), str)
        assert isinstance(event.get("stage"), str)
        if event["type"] != "error":
            assert event.get("trace_id")

    complete_event = events[-1]
    assert complete_event["type"] == "complete"
    assert complete_event["incident_id"]
    assert complete_event["trace_id"]
    assert complete_event["structured_report"]
    assert complete_event["status"] == complete_event["structured_report"]["status"]
    assert complete_event["diagnosis"]["status"] == complete_event["structured_report"]["status"]

    approval_events = [event for event in events if event["type"] == "approval_required"]
    for event in approval_events:
        assert event["pending_approval"]
        assert event["risk_assessment"]


@pytest.mark.asyncio
async def test_incident_approval_rejects_cross_incident_approval_id_without_mutation(
    monkeypatch,
    tmp_path,
) -> None:
    test_app = _build_test_app(monkeypatch, tmp_path)

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/aiops",
            json={
                "session_id": f"approval-guard-{uuid4().hex}",
                "incident": {
                    "title": "catalog-service Redis maxclients exhausted",
                    "service_name": "catalog-service",
                    "severity": "P1",
                    "symptom": "Redis 连接耗尽，需要人工确认生产动作",
                    "environment": "prod",
                    "raw_alert": {
                        "requested_action": "restart_service",
                        "reason": "人工提出重启生产服务请求",
                    },
                },
            },
            timeout=30,
        )

        events = _parse_sse_events(response.text)
        complete_event = events[-1]
        incident_id = complete_event["incident_id"]

        approval_response = await client.get(f"/api/incidents/{incident_id}/approval")
        assert approval_response.status_code == 200
        pending_approval = approval_response.json()["items"][-1]

        wrong_incident_id = f"{incident_id}-other"
        rejected_response = await client.post(
            f"/api/incidents/{wrong_incident_id}/approval",
            json={
                "approval_id": pending_approval["approval_id"],
                "decision": "approve",
                "decided_by": "pytest",
                "reason": "should be rejected before state mutation",
            },
        )

        assert rejected_response.status_code == 400
        assert (
            rejected_response.json()["detail"]
            == "approval_id does not belong to the requested incident"
        )

        approval_response_after = await client.get(f"/api/incidents/{incident_id}/approval")
        assert approval_response_after.status_code == 200
        assert approval_response_after.json()["items"][-1]["status"] == "pending"
