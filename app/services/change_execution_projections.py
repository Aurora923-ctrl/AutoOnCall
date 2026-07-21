"""Rebuildable report, incident-state, and trace projections for safe changes."""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.models.change_execution import ChangeExecution
from app.services.change_execution_read_models import build_change_execution_read_model
from app.services.incident_state_builder import build_incident_state_from_change_execution


class ChangeExecutionProjector:
    """Synchronize durable change execution state into rebuildable read projections."""

    def __init__(self, *, store: Any, report_repository: Any, trace_repository: Any):
        self._store = store
        self._report_repository = report_repository
        self._trace_repository = trace_repository

    def sync(self, execution: ChangeExecution) -> list[str]:
        pending: list[str] = []
        try:
            self._store.save_incident_state(build_incident_state_from_change_execution(execution))
        except Exception as exc:
            pending.append("incident_state")
            logger.warning(
                "Change execution incident-state projection failed: "
                "change_execution_id={}, error={}",
                execution.change_execution_id,
                exc,
            )
        if not self._sync_report(execution):
            pending.append("report")
        if not self._sync_audit(execution):
            pending.append("trace")
        return pending

    def _sync_report(self, execution: ChangeExecution) -> bool:
        try:
            self._report_repository.mark_change_execution_updated(
                incident_id=execution.incident_id,
                execution=build_change_execution_read_model(execution),
            )
            return True
        except Exception as exc:
            logger.warning(
                "Change execution report synchronization failed: incident_id={}, "
                "change_execution_id={}, error={}",
                execution.incident_id,
                execution.change_execution_id,
                exc,
            )
            return False

    def _sync_audit(self, execution: ChangeExecution) -> bool:
        event_id = (
            f"change:projection:{execution.change_execution_id}:"
            f"{execution.updated_at.isoformat().replace('+00:00', 'Z')}"
        )[:128]
        try:
            self._trace_repository.record_change_event(
                event_id=event_id,
                created_at=execution.updated_at,
                trace_id=execution.trace_id or "trace-unknown",
                incident_id=execution.incident_id,
                change_execution_id=execution.change_execution_id,
                change_plan_id=execution.change_plan_id,
                approval_id=execution.approval_id,
                event_type="change_execution_projection",
                status=execution.status,
                summary=f"Safe change durable status={execution.status}",
                metadata={"projection": True, "mode": execution.mode},
            )
            return True
        except Exception as exc:
            logger.warning(
                "Change execution audit projection failed: change_execution_id={}, error={}",
                execution.change_execution_id,
                exc,
            )
            return False
