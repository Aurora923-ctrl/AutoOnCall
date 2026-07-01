"""Alert ingestion APIs."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from loguru import logger

from app.core.auth import DIAGNOSE_SCOPE, READ_SCOPE, require_scope
from app.models.alert import (
    AlertDetailResponse,
    AlertEvent,
    AlertIngestionResult,
    AlertListResponse,
)
from app.services.aiops_service import aiops_service
from app.services.alert_ingestion_service import AlertIngestionService, alert_ingestion_service

router = APIRouter()


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
    if auto_diagnose:
        for item in result.items:
            if item.event.status == "resolved":
                continue
            background_tasks.add_task(_run_alert_diagnosis, item.event)
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
    session_id = f"alert-{event.incident_id}"
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
