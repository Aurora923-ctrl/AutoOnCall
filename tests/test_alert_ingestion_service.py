"""Tests for Alertmanager webhook normalization and Incident creation."""

from app.config import config
from app.models.incident_state import IncidentState
from app.services.alert_ingestion_service import AlertIngestionService
from app.services.sqlite_store import AIOpsSQLiteStore


def _alertmanager_payload(status: str = "firing", fingerprint: str = "fp-redis-001") -> dict:
    return {
        "receiver": "autooncall",
        "status": status,
        "commonLabels": {"environment": "prod"},
        "alerts": [
            {
                "status": status,
                "fingerprint": fingerprint,
                "labels": {
                    "alertname": "RedisMaxClientsNearLimit",
                    "service": "order-service",
                    "severity": "critical",
                },
                "annotations": {
                    "summary": "order-service Redis maxclients near limit",
                    "description": "connected_clients is above 95% of maxclients",
                },
                "startsAt": "2026-06-30T10:00:00Z",
                "endsAt": "2026-06-30T10:08:00Z" if status == "resolved" else "",
                "generatorURL": "http://prometheus.example/graph",
            }
        ],
    }


def test_alertmanager_webhook_creates_and_deduplicates_incident(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    first = service.ingest_alertmanager_webhook(_alertmanager_payload())
    second = service.ingest_alertmanager_webhook(_alertmanager_payload())

    assert first.received == 1
    assert first.created == 1
    assert first.items[0].event.status == "firing"
    assert first.items[0].event.service_name == "order-service"
    assert first.items[0].event.severity == "P1"
    assert first.items[0].incident_status == "alert_firing"
    assert second.created == 0
    assert second.deduplicated == 1
    assert second.items[0].incident_id == first.items[0].incident_id

    incident_state = store.get_incident_state(first.items[0].incident_id)
    assert incident_state is not None
    assert incident_state.status == "alert_firing"
    assert incident_state.metadata["alert_fingerprint"] == "fp-redis-001"
    assert service.get_alert_event("fp-redis-001") is not None
    assert service.list_alert_events(service_name="order-service")[0].fingerprint == "fp-redis-001"


def test_alertmanager_resolved_updates_alert_only_lifecycle(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    firing = service.ingest_alertmanager_webhook(_alertmanager_payload())
    resolved = service.ingest_alertmanager_webhook(_alertmanager_payload(status="resolved"))

    assert resolved.resolved == 1
    assert resolved.items[0].deduplicated is True
    alert = store.get_alert_event("fp-redis-001")
    assert alert is not None
    assert alert.status == "resolved"
    incident_state = store.get_incident_state(firing.items[0].incident_id)
    assert incident_state is not None
    assert incident_state.status == "resolved"
    assert incident_state.metadata["alert_status"] == "resolved"


def test_resolved_alert_does_not_override_deeper_incident_lifecycle(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    result = service.ingest_alertmanager_webhook(_alertmanager_payload())
    incident_id = result.items[0].incident_id
    store.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            status="waiting_approval",
            status_reason="human approval is pending",
            title="order-service Redis remediation",
            service_name="order-service",
            severity="P1",
            environment="prod",
            approval_status="pending",
            manual_action_required=True,
        )
    )

    service.ingest_alertmanager_webhook(_alertmanager_payload(status="resolved"))
    incident_state = store.get_incident_state(incident_id)

    assert incident_state is not None
    assert incident_state.status == "waiting_approval"
    assert incident_state.status_reason == "human approval is pending"
    assert incident_state.metadata["alert_status"] == "resolved"
    assert incident_state.metadata["preserved_incident_status"] == "waiting_approval"


def test_alert_ingestion_compacts_raw_payload_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "aiops_store_raw_external_payload", False)
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    result = service.ingest_alertmanager_webhook(_alertmanager_payload())
    raw_payload = result.items[0].event.raw_payload

    assert raw_payload["raw_truncated"] is True
    assert "alerts" not in raw_payload["webhook"]
    assert raw_payload["alert"]["fingerprint"] == "fp-redis-001"


def test_long_alertmanager_fingerprint_is_hashed_for_storage(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)
    long_fingerprint = "x" * 240

    result = service.ingest_alertmanager_webhook(
        _alertmanager_payload(fingerprint=long_fingerprint)
    )
    fingerprint = result.items[0].event.fingerprint

    assert fingerprint != long_fingerprint
    assert len(fingerprint) == 64
    assert store.get_alert_event(fingerprint) is not None
