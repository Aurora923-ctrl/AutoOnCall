"""Normalize external alerts and turn them into durable Incident states."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any

from app.config import config
from app.models.alert import AlertEvent, AlertIngestionItem, AlertIngestionResult
from app.models.incident import Incident, utc_now
from app.services.aiops_store import AIOpsStateStore, create_aiops_store
from app.services.incident_lifecycle import normalize_alert_status
from app.services.incident_state_builder import build_incident_state_from_alert

ALERT_SOURCE_ALERTMANAGER = "alertmanager"
MAX_FINGERPRINT_LENGTH = 128
MAX_SERVICE_NAME_LENGTH = 128
MAX_ENVIRONMENT_LENGTH = 64


class AlertIngestionService:
    """Ingest Alertmanager-compatible webhooks into the Incident lifecycle."""

    def __init__(self, store: AIOpsStateStore | None = None):
        self.store = store or create_aiops_store()

    def ingest_alertmanager_webhook(self, payload: dict[str, Any]) -> AlertIngestionResult:
        """Normalize and persist all alerts from an Alertmanager webhook payload."""
        alerts = _extract_alert_items(payload)
        items: list[AlertIngestionItem] = []
        created_count = 0
        deduplicated_count = 0
        resolved_count = 0

        for raw_alert in alerts:
            event = _normalize_alertmanager_alert(payload, raw_alert)
            existing = self.store.get_alert_event(event.fingerprint)
            created = existing is None
            if existing is not None:
                event.created_at = existing.created_at
                deduplicated_count += 1
            else:
                created_count += 1
            event.updated_at = utc_now()

            self.store.save_alert_event(event)
            incident = _build_incident(event)
            incident_state = self._build_incident_state(event, incident)
            self.store.save_incident_state(incident_state)
            if event.status == "resolved":
                resolved_count += 1

            items.append(
                AlertIngestionItem(
                    event=event,
                    created=created,
                    deduplicated=not created,
                    incident_id=incident.incident_id,
                    incident_status=incident_state.status,
                    status_reason=incident_state.status_reason,
                )
            )

        return AlertIngestionResult(
            source=ALERT_SOURCE_ALERTMANAGER,
            received=len(alerts),
            created=created_count,
            deduplicated=deduplicated_count,
            resolved=resolved_count,
            items=items,
        )

    def list_alert_events(
        self,
        *,
        status: str | None = None,
        service_name: str | None = None,
        limit: int = 50,
    ) -> list[AlertEvent]:
        """Return recent normalized alert events."""
        return self.store.list_alert_events(
            status=_normalize_status(status) if status else None,
            service_name=service_name,
            limit=limit,
        )

    def get_alert_event(self, fingerprint: str) -> AlertEvent | None:
        """Return one normalized alert by fingerprint."""
        return self.store.get_alert_event(fingerprint)

    def build_incident(self, event: AlertEvent) -> Incident:
        """Build the structured Incident used by the AIOps diagnosis workflow."""
        return _build_incident(event)

    def _build_incident_state(self, event: AlertEvent, incident: Incident):
        existing = self.store.get_incident_state(incident.incident_id)
        return build_incident_state_from_alert(
            event=event,
            incident=incident,
            existing=existing,
        )


def _extract_alert_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    alerts = payload.get("alerts")
    if isinstance(alerts, list):
        return [item for item in alerts if isinstance(item, dict)]
    if isinstance(payload.get("labels"), dict):
        return [payload]
    return []


def _normalize_alertmanager_alert(
    webhook_payload: dict[str, Any],
    raw_alert: dict[str, Any],
) -> AlertEvent:
    common_labels = _as_dict(webhook_payload.get("commonLabels"))
    common_annotations = _as_dict(webhook_payload.get("commonAnnotations"))
    labels = {**common_labels, **_as_dict(raw_alert.get("labels"))}
    annotations = {**common_annotations, **_as_dict(raw_alert.get("annotations"))}
    status = _normalize_status(raw_alert.get("status") or webhook_payload.get("status"))
    alertname = str(labels.get("alertname") or raw_alert.get("alertname") or "UnknownAlert")
    service_name = _truncate_text(_infer_service_name(labels), MAX_SERVICE_NAME_LENGTH)
    environment = _truncate_text(_infer_environment(labels), MAX_ENVIRONMENT_LENGTH)
    severity = _map_severity(str(labels.get("severity") or ""))
    summary = _first_non_empty(
        annotations.get("summary"),
        annotations.get("message"),
        annotations.get("description"),
        f"{alertname} firing for {service_name}",
    )
    description = _first_non_empty(annotations.get("description"), annotations.get("runbook"), "")
    fingerprint = _alert_fingerprint(
        source=ALERT_SOURCE_ALERTMANAGER,
        alertname=alertname,
        service_name=service_name,
        environment=environment,
        labels=labels,
        raw_fingerprint=raw_alert.get("fingerprint"),
    )
    return AlertEvent(
        source=ALERT_SOURCE_ALERTMANAGER,
        fingerprint=fingerprint,
        incident_id=_incident_id_from_fingerprint(fingerprint),
        status=status,
        alertname=alertname,
        service_name=service_name,
        severity=severity,
        environment=environment,
        summary=summary,
        description=description,
        labels=labels,
        annotations=annotations,
        starts_at=_parse_datetime(raw_alert.get("startsAt") or raw_alert.get("starts_at")),
        ends_at=_parse_datetime(raw_alert.get("endsAt") or raw_alert.get("ends_at")),
        generator_url=str(raw_alert.get("generatorURL") or raw_alert.get("generator_url") or ""),
        raw_payload=_raw_payload_for_storage(webhook_payload, raw_alert),
    )


def _build_incident(event: AlertEvent) -> Incident:
    symptom_parts = [event.summary]
    if event.description and event.description != event.summary:
        symptom_parts.append(event.description)
    symptom = "；".join(item for item in symptom_parts if item)
    return Incident(
        incident_id=event.incident_id,
        title=f"{event.service_name} {event.alertname}",
        service_name=event.service_name,
        severity=event.severity,
        symptom=symptom or f"{event.alertname} alert from Alertmanager",
        start_time=event.starts_at or event.created_at,
        environment=event.environment,
        raw_alert={
            "source": event.source,
            "fingerprint": event.fingerprint,
            "status": event.status,
            "alertname": event.alertname,
            "labels": event.labels,
            "annotations": event.annotations,
            "starts_at": event.starts_at.isoformat() if event.starts_at else "",
            "ends_at": event.ends_at.isoformat() if event.ends_at else "",
            "generator_url": event.generator_url,
        },
        status="investigating" if event.status != "resolved" else "resolved",
    )


def _alert_fingerprint(
    *,
    source: str,
    alertname: str,
    service_name: str,
    environment: str,
    labels: dict[str, Any],
    raw_fingerprint: Any,
) -> str:
    if raw_fingerprint:
        return _normalize_fingerprint(raw_fingerprint)
    key_labels = {
        "alertname": alertname,
        "service": service_name,
        "environment": environment,
    }
    for key in ["namespace", "pod", "instance", "job", "severity", "cluster"]:
        if labels.get(key):
            key_labels[key] = str(labels[key])
    source_text = "|".join(f"{key}={key_labels[key]}" for key in sorted(key_labels))
    return sha256(f"{source}|{source_text}".encode()).hexdigest()


def _normalize_fingerprint(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) <= MAX_FINGERPRINT_LENGTH:
        return text
    return sha256(text.encode()).hexdigest()


def _incident_id_from_fingerprint(fingerprint: str) -> str:
    digest = sha256(fingerprint.encode()).hexdigest()[:12]
    return f"inc-alert-{digest}"


def _normalize_status(value: Any) -> str:
    return normalize_alert_status(value)


def _map_severity(value: str) -> str:
    severity = value.strip().lower()
    if severity in {"p0", "p1", "critical", "page", "fatal"}:
        return "P1"
    if severity in {"p2", "warning", "warn", "major"}:
        return "P2"
    if severity in {"p3", "info", "notice", "minor"}:
        return "P3"
    if severity in {"p4", "debug"}:
        return "P4"
    return "P2"


def _infer_service_name(labels: dict[str, Any]) -> str:
    for key in [
        "service",
        "service_name",
        "app",
        "app_kubernetes_io_name",
        "app.kubernetes.io/name",
        "job",
    ]:
        value = str(labels.get(key) or "").strip()
        if value:
            return value
    return "unknown-service"


def _infer_environment(labels: dict[str, Any]) -> str:
    for key in ["environment", "env", "stage", "cluster"]:
        value = str(labels.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _raw_payload_for_storage(
    webhook_payload: dict[str, Any],
    raw_alert: dict[str, Any],
) -> dict[str, Any]:
    """Return a compact payload unless raw external storage is explicitly enabled."""
    if config.aiops_store_raw_external_payload:
        return {"webhook": webhook_payload, "alert": raw_alert}
    return {
        "raw_truncated": True,
        "webhook": {
            "receiver": webhook_payload.get("receiver", ""),
            "status": webhook_payload.get("status", ""),
            "groupLabels": _as_dict(webhook_payload.get("groupLabels")),
            "commonLabels": _as_dict(webhook_payload.get("commonLabels")),
            "commonAnnotations": _as_dict(webhook_payload.get("commonAnnotations")),
            "externalURL": webhook_payload.get("externalURL", ""),
        },
        "alert": {
            "status": raw_alert.get("status", ""),
            "fingerprint": raw_alert.get("fingerprint", ""),
            "labels": _as_dict(raw_alert.get("labels")),
            "annotations": _as_dict(raw_alert.get("annotations")),
            "startsAt": raw_alert.get("startsAt", ""),
            "endsAt": raw_alert.get("endsAt", ""),
            "generatorURL": raw_alert.get("generatorURL", ""),
        },
    }


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _truncate_text(value: str, max_length: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_length:
        return text
    return text[:max_length]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


alert_ingestion_service = AlertIngestionService()
