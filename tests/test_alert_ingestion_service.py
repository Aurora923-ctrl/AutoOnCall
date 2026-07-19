"""Tests for Alertmanager webhook normalization and Incident creation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.config import config
from app.models.alert import MAX_ALERT_URL_LENGTH
from app.models.incident_state import IncidentState
from app.services.alert_ingestion_service import (
    AlertIngestionService,
    AlertPayloadValidationError,
)
from app.services.sqlite_store import AIOpsSQLiteStore


def _alertmanager_payload(
    status: str = "firing",
    fingerprint: str = "fp-redis-001",
    *,
    starts_at: str = "2026-06-30T10:00:00Z",
    ends_at: str | None = None,
) -> dict:
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
                "startsAt": starts_at,
                "endsAt": (ends_at or "2026-06-30T10:08:00Z" if status == "resolved" else ""),
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


def test_sqlite_concurrent_alert_ingestion_creates_only_once(tmp_path) -> None:
    database_path = tmp_path / "aiops.db"
    AIOpsSQLiteStore(database_path)

    def ingest_once():
        service = AlertIngestionService(AIOpsSQLiteStore(database_path))
        return service.ingest_alertmanager_webhook(_alertmanager_payload())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: ingest_once(), range(2)))

    assert sorted(result.created for result in results) == [0, 1]
    assert sorted(result.deduplicated for result in results) == [0, 1]
    store = AIOpsSQLiteStore(database_path)
    assert len(store.list_alert_events()) == 1
    assert len(store.list_incident_states()) == 1


def test_sqlite_alert_auto_diagnosis_claim_is_exclusive_and_releasable(tmp_path) -> None:
    database_path = tmp_path / "aiops.db"
    service = AlertIngestionService(AIOpsSQLiteStore(database_path))
    incident_id = service.ingest_alertmanager_webhook(_alertmanager_payload()).items[0].incident_id
    first_store = AIOpsSQLiteStore(database_path)
    second_store = AIOpsSQLiteStore(database_path)

    first_claim = first_store.claim_alert_auto_diagnosis(incident_id)
    assert first_claim
    assert second_store.claim_alert_auto_diagnosis(incident_id) is None

    first_store.release_alert_auto_diagnosis(incident_id, first_claim)

    assert second_store.claim_alert_auto_diagnosis(incident_id)


def test_sqlite_alert_auto_diagnosis_expired_claim_can_be_reclaimed(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(config, "alert_auto_diagnosis_timeout_seconds", 30.0)
    database_path = tmp_path / "aiops.db"
    service = AlertIngestionService(AIOpsSQLiteStore(database_path))
    incident_id = service.ingest_alertmanager_webhook(_alertmanager_payload()).items[0].incident_id
    store = AIOpsSQLiteStore(database_path)
    state = store.get_incident_state(incident_id)
    assert state is not None
    metadata = dict(state.metadata)
    metadata.update(
        {
            "alert_auto_diagnosis_status": "running",
            "alert_auto_diagnosis_claimed_at": (
                datetime.now(UTC) - timedelta(minutes=5)
            ).isoformat(),
        }
    )
    store.save_incident_state(state.model_copy(update={"metadata": metadata}))

    assert AIOpsSQLiteStore(database_path).claim_alert_auto_diagnosis(incident_id)


def test_expired_alert_claim_cannot_release_newer_owner(tmp_path) -> None:
    database_path = tmp_path / "aiops.db"
    service = AlertIngestionService(AIOpsSQLiteStore(database_path))
    incident_id = service.ingest_alertmanager_webhook(_alertmanager_payload()).items[0].incident_id
    store = AIOpsSQLiteStore(database_path)

    old_claim = store.claim_alert_auto_diagnosis(incident_id)
    assert old_claim
    state = store.get_incident_state(incident_id)
    assert state is not None
    metadata = dict(state.metadata)
    metadata["alert_auto_diagnosis_claimed_at"] = (
        datetime.now(UTC) - timedelta(hours=1)
    ).isoformat()
    store.save_incident_state(state.model_copy(update={"metadata": metadata}))

    new_claim = store.claim_alert_auto_diagnosis(incident_id)
    assert new_claim and new_claim != old_claim
    store.release_alert_auto_diagnosis(incident_id, old_claim)

    current = store.get_incident_state(incident_id)
    assert current is not None
    assert current.metadata["alert_auto_diagnosis_status"] == "running"
    assert current.metadata["alert_auto_diagnosis_claim_token"] == new_claim


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
    reopened = service.ingest_alertmanager_webhook(
        _alertmanager_payload(status="firing", starts_at="2026-06-30T10:09:00Z")
    )

    item = reopened.items[0]
    assert item.created is False
    assert item.deduplicated is True
    assert item.previous_status == "resolved"
    assert item.status_changed is True
    assert item.reopened is True
    assert item.event.status == "firing"


def test_newer_firing_generation_reopens_even_when_previous_alert_is_still_firing(
    tmp_path,
) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    service.ingest_alertmanager_webhook(_alertmanager_payload())
    reopened = service.ingest_alertmanager_webhook(
        _alertmanager_payload(status="firing", starts_at="2026-06-30T10:09:00Z")
    )

    assert reopened.items[0].previous_status == "firing"
    assert reopened.items[0].reopened is True


def test_new_alert_generation_reopens_completed_incident(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)
    first = service.ingest_alertmanager_webhook(_alertmanager_payload())
    incident_id = first.items[0].incident_id
    store.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            status="completed",
            status_reason="Diagnosis report saved: completed",
            title="confirmed incident title",
            service_name="order-service",
            severity="P1",
            environment="prod",
            metadata={
                "source": "diagnosis_report",
                "starts_at": "2026-06-30T10:00:00+00:00",
            },
        )
    )
    service.ingest_alertmanager_webhook(_alertmanager_payload(status="resolved"))

    reopened = service.ingest_alertmanager_webhook(
        _alertmanager_payload(status="firing", starts_at="2026-06-30T10:09:00Z")
    )
    state = store.get_incident_state(incident_id)

    assert reopened.items[0].reopened is True
    assert state.status == "alert_firing"


def test_late_firing_event_does_not_reopen_resolved_incident(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    service.ingest_alertmanager_webhook(_alertmanager_payload())
    service.ingest_alertmanager_webhook(_alertmanager_payload(status="resolved"))
    stale = service.ingest_alertmanager_webhook(_alertmanager_payload(status="firing"))

    item = stale.items[0]
    assert item.stale_ignored is True
    assert item.reopened is False
    assert item.event.status == "resolved"
    assert item.incident_status == "resolved"
    assert store.get_alert_event("fp-redis-001").status == "resolved"


def test_older_resolve_does_not_close_newer_alert_generation(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    service.ingest_alertmanager_webhook(_alertmanager_payload(starts_at="2026-06-30T10:10:00Z"))
    stale = service.ingest_alertmanager_webhook(
        _alertmanager_payload(
            status="resolved",
            starts_at="2026-06-30T10:00:00Z",
            ends_at="2026-06-30T10:08:00Z",
        )
    )

    item = stale.items[0]
    assert item.stale_ignored is True
    assert item.status_changed is False
    assert item.event.status == "firing"
    assert item.incident_status == "alert_firing"


def test_stale_alert_does_not_replace_terminal_incident_fields(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)
    current = service.ingest_alertmanager_webhook(
        _alertmanager_payload(starts_at="2026-06-30T10:10:00Z")
    )
    incident_id = current.items[0].incident_id
    store.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            status="completed",
            status_reason="Diagnosis report saved: completed",
            title="confirmed incident title",
            service_name="order-service",
            severity="P1",
            environment="prod",
            summary="confirmed summary",
            root_cause="confirmed root cause",
            metadata={"source": "diagnosis_report"},
        )
    )
    stale_payload = _alertmanager_payload(
        status="resolved",
        starts_at="2026-06-30T10:00:00Z",
        ends_at="2026-06-30T10:08:00Z",
    )
    stale_payload["alerts"][0]["labels"]["service"] = "wrong-service"
    stale_payload["alerts"][0]["annotations"]["summary"] = "stale summary"

    stale = service.ingest_alertmanager_webhook(stale_payload)
    state = store.get_incident_state(incident_id)

    assert stale.items[0].stale_ignored is True
    assert state.status == "completed"
    assert state.title == "confirmed incident title"
    assert state.service_name == "order-service"
    assert state.summary == "confirmed summary"
    assert state.root_cause == "confirmed root cause"


def test_alert_and_incident_persistence_roll_back_together(monkeypatch, tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)

    def fail_incident_write(connection, state) -> None:
        raise RuntimeError("incident persistence failed")

    monkeypatch.setattr(store, "_save_incident_state", fail_incident_write)

    try:
        service.ingest_alertmanager_webhook(_alertmanager_payload())
    except RuntimeError as exc:
        assert str(exc) == "incident persistence failed"
    else:
        raise AssertionError("ingestion should fail when IncidentState persistence fails")

    assert store.get_alert_event("fp-redis-001") is None


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
    assert incident_state.title == "order-service Redis remediation"
    assert incident_state.service_name == "order-service"
    assert incident_state.severity == "P1"
    assert incident_state.environment == "prod"
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
    payload["alerts"][0]["annotations"]["description"] = (
        "connected_clients above threshold token=inline-secret Authorization: Bearer bearer-secret"
    )
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
    payload["alerts"][0]["annotations"]["description"] = (
        "token=inline-secret Authorization: Bearer bearer-secret"
    )
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


@pytest.mark.parametrize(
    ("mutate", "expected_message"),
    [
        (lambda payload: payload["alerts"][0].update({"status": "pending"}), "status"),
        (lambda payload: payload["alerts"][0].update({"startsAt": "not-a-time"}), "startsAt"),
        (
            lambda payload: payload["alerts"][0].update({"startsAt": "2026-06-30T10:00:00"}),
            "timezone",
        ),
        (
            lambda payload: payload["alerts"][0].update(
                {
                    "status": "resolved",
                    "startsAt": "2026-06-30T10:10:00Z",
                    "endsAt": "2026-06-30T10:08:00Z",
                }
            ),
            "endsAt",
        ),
        (
            lambda payload: payload["alerts"][0]["labels"].pop("alertname"),
            "alertname",
        ),
    ],
)
def test_invalid_alert_lifecycle_fields_are_rejected_before_persistence(
    mutate,
    expected_message,
    tmp_path,
) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)
    payload = _alertmanager_payload()
    mutate(payload)

    with pytest.raises(AlertPayloadValidationError, match=expected_message):
        service.ingest_alertmanager_webhook(payload)

    assert service.list_alert_events() == []
    assert store.list_incident_states() == []


def test_mixed_batch_is_fully_validated_before_any_alert_is_persisted(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)
    payload = _alertmanager_payload()
    invalid_alert = {
        **payload["alerts"][0],
        "fingerprint": "fp-invalid-second",
        "startsAt": "not-a-time",
    }
    payload["alerts"].append(invalid_alert)

    with pytest.raises(AlertPayloadValidationError, match=r"alerts\[1\].*startsAt"):
        service.ingest_alertmanager_webhook(payload)

    assert service.list_alert_events() == []
    assert store.list_incident_states() == []


def test_non_object_alert_item_is_rejected_instead_of_silently_skipped(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)
    payload = _alertmanager_payload()
    payload["alerts"].append("not-an-alert")

    with pytest.raises(AlertPayloadValidationError, match=r"alerts\[1\]"):
        service.ingest_alertmanager_webhook(payload)

    assert service.list_alert_events() == []


def test_alertmanager_zero_time_ends_at_is_treated_as_open_firing_alert(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    service = AlertIngestionService(store)
    payload = _alertmanager_payload()
    payload["alerts"][0]["endsAt"] = "0001-01-01T00:00:00Z"

    result = service.ingest_alertmanager_webhook(payload)

    assert result.items[0].event.ends_at is None
