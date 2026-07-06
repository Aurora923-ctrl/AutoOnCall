"""API tests for alert ingestion and Incident list integration."""

import importlib

import httpx
import pytest
from fastapi import FastAPI

from app.api import alerts, incidents
from app.services.alert_ingestion_service import AlertIngestionService
from app.services.approval_service import ApprovalService
from app.services.report_generator import ReportGenerator
from app.services.sqlite_store import AIOpsSQLiteStore
from app.services.trace_service import TraceService


def _payload(status: str = "firing") -> dict:
    return {
        "receiver": "autooncall",
        "status": status,
        "alerts": [
            {
                "status": status,
                "fingerprint": "fp-api-001",
                "labels": {
                    "alertname": "HighErrorRate",
                    "service": "checkout-service",
                    "severity": "warning",
                    "environment": "prod",
                },
                "annotations": {
                    "summary": "checkout-service 5xx error rate is high",
                },
                "startsAt": "2026-06-30T11:00:00Z",
            }
        ],
    }


def _build_test_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> FastAPI:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    alert_service = AlertIngestionService(store)
    report_store = ReportGenerator(tmp_path / "reports.db")
    trace_store = TraceService(tmp_path / "traces.db")
    approval_store = ApprovalService(tmp_path / "approvals.db")
    incidents_api = importlib.import_module("app.api.incidents")

    monkeypatch.setattr(alerts, "get_alert_ingestion_service", lambda: alert_service)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: store)
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: report_store)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: trace_store)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approval_store)

    app = FastAPI()
    app.include_router(alerts.router, prefix="/api")
    app.include_router(incidents.router, prefix="/api")
    return app


@pytest.mark.asyncio
async def test_alertmanager_webhook_creates_incident_visible_in_list(monkeypatch, tmp_path) -> None:
    app = _build_test_app(monkeypatch, tmp_path)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post("/api/alerts/alertmanager", json=_payload())
        duplicate = await client.post("/api/alerts/alertmanager", json=_payload())
        alerts_response = await client.get("/api/alerts?service_name=checkout-service")
        incidents_response = await client.get("/api/incidents")

    assert created.status_code == 200
    created_payload = created.json()
    assert created_payload["created"] == 1
    assert created_payload["items"][0]["event"]["severity"] == "P2"
    assert created_payload["items"][0]["incident_status"] == "alert_firing"
    incident_id = created_payload["items"][0]["incident_id"]

    assert duplicate.status_code == 200
    assert duplicate.json()["deduplicated"] == 1

    assert alerts_response.status_code == 200
    assert alerts_response.json()["items"][0]["fingerprint"] == "fp-api-001"

    assert incidents_response.status_code == 200
    incidents_payload = incidents_response.json()
    assert incidents_payload["items"][0]["incident_id"] == incident_id
    assert incidents_payload["items"][0]["status"] == "alert_firing"
    assert incidents_payload["items"][0]["service_name"] == "checkout-service"


@pytest.mark.asyncio
async def test_alertmanager_webhook_rejects_payload_without_alerts(monkeypatch, tmp_path) -> None:
    app = _build_test_app(monkeypatch, tmp_path)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/alerts/alertmanager", json={"receiver": "autooncall"})

    assert response.status_code == 400
    assert "valid alerts" in response.json()["detail"]


@pytest.mark.asyncio
async def test_alertmanager_auto_diagnose_only_runs_for_new_alert(
    monkeypatch,
    tmp_path,
) -> None:
    app = _build_test_app(monkeypatch, tmp_path)
    diagnosis_calls = []

    async def fake_run_alert_diagnosis(event):
        diagnosis_calls.append(event.fingerprint)

    monkeypatch.setattr(alerts, "_run_alert_diagnosis", fake_run_alert_diagnosis)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post("/api/alerts/alertmanager?auto_diagnose=true", json=_payload())
        duplicate = await client.post(
            "/api/alerts/alertmanager?auto_diagnose=true",
            json=_payload(),
        )

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert first.json()["created"] == 1
    assert duplicate.json()["deduplicated"] == 1
    assert diagnosis_calls == ["fp-api-001"]


@pytest.mark.asyncio
async def test_alertmanager_auto_diagnose_runs_for_reopened_alert(
    monkeypatch,
    tmp_path,
) -> None:
    app = _build_test_app(monkeypatch, tmp_path)
    diagnosis_calls = []

    async def fake_run_alert_diagnosis(event):
        diagnosis_calls.append((event.fingerprint, event.status))

    monkeypatch.setattr(alerts, "_run_alert_diagnosis", fake_run_alert_diagnosis)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post("/api/alerts/alertmanager?auto_diagnose=true", json=_payload())
        resolved = await client.post(
            "/api/alerts/alertmanager?auto_diagnose=true",
            json=_payload(status="resolved"),
        )
        reopened = await client.post(
            "/api/alerts/alertmanager?auto_diagnose=true",
            json=_payload(status="firing"),
        )

    assert first.status_code == 200
    assert resolved.status_code == 200
    assert reopened.status_code == 200
    assert reopened.json()["items"][0]["reopened"] is True
    assert diagnosis_calls == [("fp-api-001", "firing"), ("fp-api-001", "firing")]


@pytest.mark.asyncio
async def test_alertmanager_auto_diagnose_skips_incident_already_in_flight(
    monkeypatch,
    tmp_path,
) -> None:
    app = _build_test_app(monkeypatch, tmp_path)
    diagnosis_calls = []

    async def fake_run_alert_diagnosis(event):
        diagnosis_calls.append(event.fingerprint)

    seed_service = AlertIngestionService(AIOpsSQLiteStore(tmp_path / "seed.db"))
    incident_id = seed_service.ingest_alertmanager_webhook(_payload()).items[0].event.incident_id
    alerts._mark_alert_diagnosis_in_flight(incident_id)
    monkeypatch.setattr(alerts, "_run_alert_diagnosis", fake_run_alert_diagnosis)

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/alerts/alertmanager?auto_diagnose=true",
                json=_payload(),
            )
    finally:
        alerts._clear_alert_diagnosis_in_flight(incident_id)

    assert response.status_code == 200
    assert diagnosis_calls == []


@pytest.mark.asyncio
async def test_alert_auto_diagnosis_uses_unique_session_ids(monkeypatch, tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    alert_service = AlertIngestionService(store)
    event = alert_service.ingest_alertmanager_webhook(_payload()).items[0].event
    session_ids = []

    class FakeAIOpsService:
        async def diagnose(self, session_id=None, incident=None):
            session_ids.append(session_id)
            yield {"type": "complete"}

    monkeypatch.setattr(alerts, "get_alert_ingestion_service", lambda: alert_service)
    monkeypatch.setattr(alerts, "aiops_service", FakeAIOpsService())

    await alerts._run_alert_diagnosis(event)
    await alerts._run_alert_diagnosis(event)

    assert len(session_ids) == 2
    assert all(session_id.startswith(f"alert-{event.incident_id}-") for session_id in session_ids)
    assert session_ids[0] != session_ids[1]
    assert f"alert-{event.incident_id}" not in session_ids


@pytest.mark.asyncio
async def test_alert_auto_diagnosis_failure_updates_incident_state(monkeypatch, tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    alert_service = AlertIngestionService(store)
    event = alert_service.ingest_alertmanager_webhook(_payload()).items[0].event

    class FailingAIOpsService:
        async def diagnose(self, session_id=None, incident=None):
            raise RuntimeError("planner unavailable")
            yield  # pragma: no cover

    monkeypatch.setattr(alerts, "get_alert_ingestion_service", lambda: alert_service)
    monkeypatch.setattr(alerts, "aiops_service", FailingAIOpsService())
    monkeypatch.setattr(alerts, "trace_service", TraceService(tmp_path / "traces.db"))

    await alerts._run_alert_diagnosis(event)

    state = store.get_incident_state(event.incident_id)
    assert state is not None
    assert state.status == "failed"
    assert "planner unavailable" not in state.status_reason
    assert "诊断服务暂时不可用" in state.status_reason
    assert state.metadata["alert_auto_diagnosis_status"] == "failed"
    assert "planner unavailable" not in state.metadata["alert_auto_diagnosis_error"]


@pytest.mark.asyncio
async def test_get_alert_returns_404_for_unknown_fingerprint(monkeypatch, tmp_path) -> None:
    app = _build_test_app(monkeypatch, tmp_path)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/alerts/missing")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_alert_list_rejects_unknown_status_filter(monkeypatch, tmp_path) -> None:
    app = _build_test_app(monkeypatch, tmp_path)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/alerts?status=bogus")

    assert response.status_code == 422
