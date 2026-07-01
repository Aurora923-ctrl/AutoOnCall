"""Storage factory for AIOps runtime state."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.config import config
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.alert import AlertEvent
from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.mysql_store import AIOpsMySQLStore
from app.services.sqlite_store import AIOpsSQLiteStore


class AIOpsStateStore(Protocol):
    """Repository contract shared by SQLite and MySQL stores."""

    def save_alert_event(self, event: AlertEvent) -> None: ...

    def get_alert_event(self, fingerprint: str) -> AlertEvent | None: ...

    def list_alert_events(
        self,
        *,
        status: str | None = None,
        service_name: str | None = None,
        limit: int = 50,
    ) -> list[AlertEvent]: ...

    def save_trace_event(self, event: TraceEvent) -> None: ...

    def list_trace_events(
        self,
        *,
        incident_id: str | None = None,
        trace_id: str | None = None,
        event_type: str | None = None,
    ) -> list[TraceEvent]: ...

    def save_approval_request(self, request: ApprovalRequest) -> None: ...

    def save_approval_decision_if_pending(self, request: ApprovalRequest) -> bool: ...

    def get_approval_request(self, approval_id: str) -> ApprovalRequest | None: ...

    def list_approval_requests(
        self,
        *,
        incident_id: str | None = None,
        status: str | None = None,
    ) -> list[ApprovalRequest]: ...

    def save_change_execution(self, execution: ChangeExecution) -> None: ...

    def create_change_execution_once(
        self,
        execution: ChangeExecution,
    ) -> tuple[ChangeExecution, bool]: ...

    def get_change_execution(self, change_execution_id: str) -> ChangeExecution | None: ...

    def list_change_executions(
        self,
        *,
        incident_id: str | None = None,
        change_plan_id: str | None = None,
    ) -> list[ChangeExecution]: ...

    def save_aiops_session_snapshot(self, snapshot: AIOpsSessionSnapshot) -> None: ...

    def get_aiops_session_snapshot(self, session_id: str) -> AIOpsSessionSnapshot | None: ...

    def get_latest_aiops_session_snapshot(
        self,
        incident_id: str,
    ) -> AIOpsSessionSnapshot | None: ...

    def list_aiops_session_snapshots(
        self,
        *,
        incident_id: str | None = None,
        limit: int = 20,
    ) -> list[AIOpsSessionSnapshot]: ...

    def save_incident_state(self, state: IncidentState) -> None: ...

    def get_incident_state(self, incident_id: str) -> IncidentState | None: ...

    def list_incident_states(self) -> list[IncidentState]: ...

    def save_report(self, report: DiagnosisReport) -> None: ...

    def get_latest_report(self, incident_id: str) -> DiagnosisReport | None: ...

    def list_latest_reports(self) -> list[DiagnosisReport]: ...

    def cleanup_older_than(self, *, keep_days: int, dry_run: bool = False) -> dict: ...


def create_aiops_store(
    storage_path: str | Path | None = None,
    *,
    backend: str | None = None,
) -> AIOpsStateStore:
    """Create the configured AIOps runtime store.

    Passing ``storage_path`` keeps the historical behavior used by tests and local
    tools: an explicit path always means SQLite. Production deployments can switch
    the default singleton services with ``AIOPS_STORAGE_BACKEND=mysql``.
    """
    if storage_path is not None:
        return AIOpsSQLiteStore(storage_path)

    selected_backend = (backend or config.aiops_storage_backend or "sqlite").strip().lower()
    if selected_backend == "mysql":
        return AIOpsMySQLStore()
    if selected_backend == "sqlite":
        return AIOpsSQLiteStore()
    raise ValueError(f"Unsupported AIOps storage backend: {selected_backend}")
