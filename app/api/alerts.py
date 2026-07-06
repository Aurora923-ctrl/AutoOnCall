"""Alert ingestion APIs."""

from __future__ import annotations

import asyncio
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from loguru import logger

from app.core.auth import DIAGNOSE_SCOPE, READ_SCOPE, require_scope
from app.models.alert import (
    AlertDetailResponse,
    AlertEvent,
    AlertIngestionResult,
    AlertListResponse,
)
from app.models.incident_state import IncidentState
from app.services.aiops_service import aiops_service
from app.services.alert_ingestion_service import AlertIngestionService, alert_ingestion_service
from app.services.trace_service import trace_service
from app.utils.public_errors import GENERIC_DIAGNOSIS_ERROR, public_exception_message

router = APIRouter()
ALERT_AUTO_DIAGNOSIS_MAX_CONCURRENCY = 2
_alert_auto_diagnosis_semaphore = asyncio.Semaphore(ALERT_AUTO_DIAGNOSIS_MAX_CONCURRENCY)
_alert_auto_diagnosis_lock = Lock()
_alert_auto_diagnosis_in_flight: set[str] = set()


def get_alert_ingestion_service() -> AlertIngestionService:
    """Return the alert ingestion singleton."""
    return alert_ingestion_service


@router.post(
    "/alerts/alertmanager",
    response_model=AlertIngestionResult,
    dependencies=[Depends(require_scope(DIAGNOSE_SCOPE))],
)
async def ingest_alertmanager_webhook(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    auto_diagnose: bool = Query(default=False),
) -> AlertIngestionResult:
    """Ingest Alertmanager webhook payloads and create/update Incident state."""
    service = get_alert_ingestion_service()
    result = service.ingest_alertmanager_webhook(payload)
    if result.received == 0:
        raise HTTPException(
            status_code=400,
            detail="Alertmanager payload does not contain any valid alerts",
        )
    if auto_diagnose:
        for item in result.items:
            if item.event.status == "resolved" or not (item.created or item.reopened):
                continue
            if _mark_alert_diagnosis_in_flight(item.event.incident_id):
                background_tasks.add_task(_run_alert_diagnosis_guarded, item.event)
            else:
                logger.info(
                    "Alert auto diagnosis already running: incident_id={}, fingerprint={}",
                    item.event.incident_id,
                    item.event.fingerprint,
                )
    return result


@router.get(
    "/alerts",
    response_model=AlertListResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def list_alerts(
    status: (
        Literal["firing", "resolved", "active", "triggered", "inactive", "ok", "closed"] | None
    ) = Query(default=None),
    service_name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> AlertListResponse:
    """Return recently ingested normalized alerts."""
    items = get_alert_ingestion_service().list_alert_events(
        status=status,
        service_name=service_name,
        limit=limit,
    )
    return AlertListResponse(items=items)


@router.get(
    "/alerts/{fingerprint}",
    response_model=AlertDetailResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def get_alert(fingerprint: str) -> AlertDetailResponse:
    """Return one normalized alert by fingerprint."""
    alert = get_alert_ingestion_service().get_alert_event(fingerprint)
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return AlertDetailResponse(alert=alert)


async def _run_alert_diagnosis(event: AlertEvent) -> None:
    """Run the normal AIOps workflow in the background for one firing alert."""
    service = get_alert_ingestion_service()
    incident = service.build_incident(event)
    session_id = f"alert-{event.incident_id}-{uuid4().hex}"
    try:
        async for _ in aiops_service.diagnose(session_id=session_id, incident=incident):
            pass
    except Exception as exc:
        logger.error(
            "Alert auto diagnosis failed: incident_id={}, fingerprint={}, error={}",
            event.incident_id,
            event.fingerprint,
            exc,
            exc_info=True,
        )
        _record_alert_diagnosis_failure(service, event, session_id, exc)


async def _run_alert_diagnosis_guarded(event: AlertEvent) -> None:
    """Run one auto diagnosis with process-local dedupe and backpressure."""
    try:
        async with _alert_auto_diagnosis_semaphore:
            await _run_alert_diagnosis(event)
    finally:
        _clear_alert_diagnosis_in_flight(event.incident_id)


def _mark_alert_diagnosis_in_flight(incident_id: str) -> bool:
    """Return True when this process should start a new alert diagnosis."""
    with _alert_auto_diagnosis_lock:
        if incident_id in _alert_auto_diagnosis_in_flight:
            return False
        _alert_auto_diagnosis_in_flight.add(incident_id)
        return True


def _clear_alert_diagnosis_in_flight(incident_id: str) -> None:
    """Clear the process-local in-flight marker for an alert diagnosis."""
    with _alert_auto_diagnosis_lock:
        _alert_auto_diagnosis_in_flight.discard(incident_id)


def _record_alert_diagnosis_failure(
    service: AlertIngestionService,
    event: AlertEvent,
    session_id: str,
    exc: Exception,
) -> None:
    """Persist visible failure state for background alert diagnosis."""
    public_error = public_exception_message(exc, fallback=GENERIC_DIAGNOSIS_ERROR)
    existing = service.store.get_incident_state(event.incident_id)
    trace_id = existing.trace_id if existing and existing.trace_id else f"trace-{event.incident_id}"
    trace_event = trace_service.create_event(
        trace_id=trace_id,
        incident_id=event.incident_id,
        node_name="workflow",
        event_type="workflow_error",
        input_summary=f"alert_fingerprint={event.fingerprint}",
        output_summary="Alert auto diagnosis failed",
        status="failed",
        error_message=public_error,
        metadata={
            "session_id": session_id,
            "alert_fingerprint": event.fingerprint,
            "alert_source": event.source,
        },
    )
    metadata = dict(existing.metadata if existing else {})
    metadata.update(
        {
            "alert_auto_diagnosis_status": "failed",
            "alert_auto_diagnosis_error": public_error,
            "alert_auto_diagnosis_session_id": session_id,
            "alert_auto_diagnosis_trace_event_id": trace_event.event_id,
        }
    )
    state = (
        existing
        if existing is not None
        else IncidentState(
            incident_id=event.incident_id,
            title=f"{event.service_name} {event.alertname}",
            service_name=event.service_name,
            severity=event.severity,
            environment=event.environment,
            summary=event.summary,
        )
    )
    service.store.save_incident_state(
        state.model_copy(
            update={
                "status": "failed",
                "status_reason": f"Alert auto diagnosis failed: {public_error}",
                "trace_id": trace_id,
                "session_id": session_id,
                "metadata": metadata,
            }
        )
    )
