"""API tests for the safe change workflow endpoints."""

from __future__ import annotations

import importlib
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api import aiops
from app.models.approval import ApprovalRequest
from app.services.approval_service import ApprovalService
from app.services.change_execution_service import ChangeExecutionService
from app.services.change_plan_builder import build_change_plan
from app.services.report_generator import ReportGenerator
from app.services.trace_service import TraceService


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


def _build_test_app(monkeypatch: pytest.MonkeyPatch, tmp_path):
    test_app = FastAPI()
    test_app.include_router(aiops.router, prefix="/api")

    database_path = tmp_path / "safe-change-api.db"
    trace_store = TraceService(database_path)
    report_store = ReportGenerator(database_path)
    approval_store = ApprovalService(database_path, sync_report_status=False)
    change_service = ChangeExecutionService(
        database_path,
        approval_repository=approval_store,
        trace_repository=trace_store,
        report_repository=report_store,
    )

    aiops_api = importlib.import_module("app.api.aiops")
    monkeypatch.setattr(aiops_api, "get_change_execution_service", lambda: change_service)
    return test_app, approval_store, change_service


def _approved_request(approval_store: ApprovalService):
    plan = build_change_plan(
        incident_id="inc-api",
        action="人工调整 Redis maxclients",
        risk_level="high",
        tool_name="suggest_remediation",
        service_name="order-service",
        environment="prod",
        metadata={"trace_id": "trace-api"},
    )
    request = approval_store.create_request(
        ApprovalRequest(
            incident_id="inc-api",
            action=plan.action,
            risk_level="high",
            reason="生产变更需要审批",
            change_plan=plan,
            metadata={"trace_id": "trace-api", "change_plan": plan.model_dump(mode="json")},
        )
    )
    approval = approval_store.decide_request(
        approval_id=request.approval_id,
        decision="approve",
        decided_by="pytest",
        reason="api test approval",
    )
    assert approval.change_plan is not None
    return approval, approval.change_plan


@pytest.mark.asyncio
async def test_safe_change_resume_api_streams_precheck_dry_run_and_complete(
    monkeypatch,
    tmp_path,
) -> None:
    test_app, approval_store, _ = _build_test_app(monkeypatch, tmp_path)
    approval, plan = _approved_request(approval_store)

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/incidents/{approval.incident_id}/changes/{plan.change_plan_id}/resume",
            json={
                "approval_id": approval.approval_id,
                "mode": "dry_run_only",
                "operator": "pytest",
            },
        )
        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        assert events[-1]["type"] == "complete"
        assert events[-1]["status"] == "closed"
        assert "change_precheck" in [event["type"] for event in events]
        assert "change_dry_run" in [event["type"] for event in events]

        list_response = await client.get(f"/api/incidents/{approval.incident_id}/changes")
        assert list_response.status_code == 200
        items = list_response.json()["items"]
        assert len(items) == 1
        assert items[0]["status_metadata"]["status"] == "resolved"
        assert [stage["key"] for stage in items[0]["stages"]] == [
            "pre_check",
            "dry_run",
            "execute",
            "observe",
        ]
        assert items[0]["stages"][2]["status"] == "skipped"

        detail_response = await client.get(f"/api/changes/{items[0]['change_execution_id']}")
        assert detail_response.status_code == 200
        detail = detail_response.json()["change_execution"]
        assert detail["status"] == "closed"
        assert detail["lifecycle_status"] == "resolved"
        assert detail["stages"][0]["status"] == "passed"


@pytest.mark.asyncio
async def test_manual_change_result_api_records_observation(monkeypatch, tmp_path) -> None:
    test_app, approval_store, _ = _build_test_app(monkeypatch, tmp_path)
    approval, plan = _approved_request(approval_store)

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/incidents/{approval.incident_id}/changes/{plan.change_plan_id}/resume",
            json={
                "approval_id": approval.approval_id,
                "mode": "manual_record",
                "operator": "pytest",
            },
        )
        events = _parse_sse_events(response.text)
        execution_id = events[-1]["change_execution"]["change_execution_id"]
        assert events[-1]["status"] == "waiting_manual_execution"

        result_response = await client.post(
            f"/api/changes/{execution_id}/manual-result",
            json={
                "status": "succeeded",
                "operator": "pytest",
                "notes": "人工执行完成，观察正常",
                "observed_metrics": {"service_5xx_rate": 0},
            },
        )

    assert result_response.status_code == 200
    payload = result_response.json()["change_execution"]
    assert payload["status"] == "closed"
    assert payload["observation"]["status"] == "passed"
    assert payload["manual_result_required"] is False
    assert payload["stages"][2]["status"] == "closed"
