"""Tests for Alertmanager webhook normalization and Incident creation."""

from app.config import config
from app.models.alert import MAX_ALERT_URL_LENGTH
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
    assert resolved.items[0].previous_status == "firing"
    assert resolved.items[0].status_changed is True
    assert resolved.items[0].reopened is False
    alert = store.get_alert_event("fp-redis-001")
    assert alert is not None
    assert alert.status == "resolved"
    incident_state = store.get_incident_state(firing.items[0].incident_id)
    assert incident_state is not None
    assert incident_state.status == "resolved"
    assert incident_state.metadata["alert_status"] == "resolved"


def test_alertmanager_reopened_firing_alert_is_marked_for_diagnosis(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    service.ingest_alertmanager_webhook(_alertmanager_payload())
    service.ingest_alertmanager_webhook(_alertmanager_payload(status="resolved"))
    reopened = service.ingest_alertmanager_webhook(_alertmanager_payload(status="firing"))

    item = reopened.items[0]
    assert item.created is False
    assert item.deduplicated is True
    assert item.previous_status == "resolved"
    assert item.status_changed is True
    assert item.reopened is True
    assert item.event.status == "firing"


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


def test_resolved_alert_can_recover_auto_diagnosis_failed_lifecycle(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    result = service.ingest_alertmanager_webhook(_alertmanager_payload())
    incident_id = result.items[0].incident_id
    store.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            status="failed",
            status_reason="Alert auto diagnosis failed",
            title="order-service Redis remediation",
            service_name="order-service",
            severity="P1",
            environment="prod",
            metadata={"alert_auto_diagnosis_status": "failed"},
        )
    )

    service.ingest_alertmanager_webhook(_alertmanager_payload(status="resolved"))
    incident_state = store.get_incident_state(incident_id)

    assert incident_state is not None
    assert incident_state.status == "resolved"
    assert incident_state.metadata["alert_status"] == "resolved"
    assert incident_state.metadata["alert_auto_diagnosis_status"] == "failed"


def test_alert_ingestion_compacts_raw_payload_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "aiops_store_raw_external_payload", False)
    payload = _alertmanager_payload()
    payload["externalURL"] = "https://alertmanager.example/" + "x" * 3000
    payload["alerts"][0]["generatorURL"] = "https://prometheus.example/graph?" + "q=" + "y" * 3000
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    result = service.ingest_alertmanager_webhook(payload)
    raw_payload = result.items[0].event.raw_payload

    assert raw_payload["raw_truncated"] is True
    assert "alerts" not in raw_payload["webhook"]
    assert raw_payload["alert"]["fingerprint"] == "fp-redis-001"
    assert len(raw_payload["webhook"]["externalURL"]) == MAX_ALERT_URL_LENGTH
    assert len(raw_payload["alert"]["generatorURL"]) == MAX_ALERT_URL_LENGTH


def test_alert_ingestion_redacts_sensitive_labels_and_annotations(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(config, "aiops_store_raw_external_payload", False)
    payload = _alertmanager_payload()
    payload["commonLabels"]["api_token"] = "secret-token"
    payload["alerts"][0]["labels"]["password"] = "redis-password"
    payload["alerts"][0]["annotations"]["authorization"] = "Bearer secret"
    payload["alerts"][0]["annotations"][
        "description"
    ] = "connected_clients above threshold token=inline-secret Authorization: Bearer bearer-secret"
    payload["alerts"][0]["annotations"]["details"] = {
        "message": "redis dsn=mysql://user:secret@db.local access_key=access-secret"
    }
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    result = service.ingest_alertmanager_webhook(payload)
    event = result.items[0].event

    description = event.annotations["description"]
    assert event.labels["api_token"] == "[REDACTED]"
    assert event.labels["password"] == "[REDACTED]"
    assert event.annotations["authorization"] == "[REDACTED]"
    assert "inline-secret" not in description
    assert "bearer-secret" not in description
    assert "token=[REDACTED]" in description
    assert "Authorization: [REDACTED]" in description
    assert "inline-secret" not in event.description
    assert "bearer-secret" not in event.description
    assert event.annotations["details"]["message"] == ("redis dsn=[REDACTED] access_key=[REDACTED]")
    assert event.service_name == "order-service"
    assert event.raw_payload["webhook"]["commonLabels"]["api_token"] == "[REDACTED]"
    assert event.raw_payload["alert"]["labels"]["password"] == "[REDACTED]"
    assert event.raw_payload["alert"]["annotations"]["authorization"] == "[REDACTED]"
    assert "inline-secret" not in event.raw_payload["alert"]["annotations"]["description"]


def test_alert_ingestion_redacts_full_raw_payload_when_enabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "aiops_store_raw_external_payload", True)
    payload = _alertmanager_payload()
    payload["alerts"][0]["annotations"][
        "description"
    ] = "token=inline-secret Authorization: Bearer bearer-secret"
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    result = service.ingest_alertmanager_webhook(payload)
    raw_payload = result.items[0].event.raw_payload

    webhook_description = raw_payload["webhook"]["alerts"][0]["annotations"]["description"]
    alert_description = raw_payload["alert"]["annotations"]["description"]
    assert "inline-secret" not in webhook_description
    assert "bearer-secret" not in webhook_description
    assert "inline-secret" not in alert_description
    assert "bearer-secret" not in alert_description


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


def test_blank_alertmanager_fingerprint_falls_back_to_stable_hash(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)
    first_payload = _alertmanager_payload(fingerprint="   ")
    second_payload = _alertmanager_payload(fingerprint="   ")
    first_payload["alerts"][0]["labels"]["instance"] = "redis-a"
    second_payload["alerts"][0]["labels"]["instance"] = "redis-b"

    first = service.ingest_alertmanager_webhook(first_payload)
    second = service.ingest_alertmanager_webhook(second_payload)
    first_fingerprint = first.items[0].event.fingerprint
    second_fingerprint = second.items[0].event.fingerprint

    assert first.created == 1
    assert second.created == 1
    assert first_fingerprint
    assert second_fingerprint
    assert first_fingerprint != "   "
    assert first_fingerprint != second_fingerprint
    assert len(first_fingerprint) == 64
    assert len(second_fingerprint) == 64
