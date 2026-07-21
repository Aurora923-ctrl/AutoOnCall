"""Facade for building, persisting, and querying diagnosis reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.aiops_state_utils import extract_incident_id
from app.services.aiops_store import create_aiops_store
from app.services.incident_lifecycle import incident_status_from_report_status
from app.services.incident_state_builder import build_incident_state_from_report
from app.services.legacy_migration import resolve_legacy_jsonl_path
from app.services.report_builder import ReportBuilder
from app.services.report_lifecycle import ReportLifecycle
from app.services.report_markdown import render_markdown
from app.services.sqlite_store import resolve_sqlite_path
from app.services.trace_service import TraceService, trace_service
from app.utils.redaction import redact_sensitive_data


class ReportGenerator:
    """Stable facade for report construction, persistence, and lifecycle updates."""

    def __init__(
        self,
        storage_path: str | Path | None = None,
        *,
        legacy_storage_path: str | Path | None = None,
    ):
        raw_storage_path = Path(storage_path) if storage_path is not None else None
        self.database_path = resolve_sqlite_path(raw_storage_path)
        self._store = create_aiops_store(raw_storage_path)
        self.storage_path = getattr(self._store, "storage_path", self.database_path)
        self._trace_service = _trace_repository_for_storage(raw_storage_path)
        self._builder = ReportBuilder()
        self._lifecycle = ReportLifecycle()
        self._migrate_legacy_jsonl(
            legacy_storage_path
            if legacy_storage_path is not None
            else resolve_legacy_jsonl_path(raw_storage_path, "reports.jsonl")
        )

    def generate_from_state(
        self,
        state: dict[str, Any],
        *,
        trace_events: list[TraceEvent] | None = None,
        status: str = "completed",
    ) -> DiagnosisReport:
        """Build and persist a deterministic report from the current workflow state."""
        incident_id = extract_incident_id(state)
        trace_id = str(state.get("trace_id") or "")
        events = (
            list(trace_events)
            if trace_events is not None
            else self._trace_service.list_events(
                incident_id=incident_id,
                trace_id=trace_id or None,
            )
        )
        report = self._builder.build_from_state(
            state,
            trace_events=events,
            status=status,
        )
        report = self.save_report(report)
        if trace_events is None:
            self._record_report_generated(report, existing_events=events)
        return report

    def save_report(self, report: DiagnosisReport) -> DiagnosisReport:
        """Persist a sanitized report together with its incident projection."""
        report = _sanitize_report(report)
        incident_state = build_incident_state_from_report(
            report=report,
            status=incident_status_from_report_status(report.status),
            status_reason=f"Diagnosis report saved: {report.status}",
        )
        self._store.save_report_with_incident(report, incident_state)
        return report

    def get_report(self, incident_id: str) -> DiagnosisReport | None:
        """Return the latest report for one incident."""
        return self._store.get_latest_report(incident_id)

    def mark_approval_decided(
        self,
        *,
        incident_id: str,
        approval_status: str,
        decided_by: str | None = None,
        reason: str = "",
        approval_request: dict[str, Any] | None = None,
    ) -> DiagnosisReport | None:
        """Apply and persist an approval decision on the latest report."""
        report = self.get_report(incident_id)
        if report is None:
            return None
        updated = self._lifecycle.apply_approval_decision(
            report,
            approval_status=approval_status,
            decided_by=decided_by,
            reason=reason,
            approval_request=approval_request,
        )
        return report if updated is report else self.save_report(updated)

    def mark_change_execution_updated(
        self,
        *,
        incident_id: str,
        execution: dict[str, Any],
    ) -> DiagnosisReport | None:
        """Apply and persist the latest safe-change workflow state."""
        report = self.get_report(incident_id)
        if report is None:
            return None
        return self.save_report(
            self._lifecycle.apply_change_execution(
                report,
                execution=execution,
            )
        )

    def list_reports(self) -> list[DiagnosisReport]:
        """Return all latest reports sorted by creation time."""
        return self._store.list_latest_reports()

    def _record_report_generated(
        self,
        report: DiagnosisReport,
        *,
        existing_events: list[TraceEvent],
    ) -> None:
        """Append report persistence to this generator's trace stream."""
        if not report.trace_id or not existing_events:
            return
        if any(
            event.event_type == "report_generated"
            and event.metadata.get("report_id") == report.report_id
            for event in existing_events
        ):
            return
        self._trace_service.create_event(
            trace_id=report.trace_id,
            incident_id=report.incident_id,
            node_name="report_generator",
            event_type="report_generated",
            output_summary=f"report_id={report.report_id}, status={report.status}",
            status=report.status,
            metadata={
                "report_id": report.report_id,
                "approval_status": report.approval_status,
                "manual_action_required": report.manual_action_required,
            },
        )

    def _migrate_legacy_jsonl(self, legacy_storage_path: str | Path | None) -> None:
        if legacy_storage_path is None:
            return
        path = Path(legacy_storage_path)
        if not path.exists():
            return
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                payload = record.get("report") or record
                report = DiagnosisReport.model_validate(payload)
            except Exception as exc:
                logger.warning(
                    "Skipping invalid legacy report record: path={}, line={}, error={}",
                    path,
                    line_number,
                    exc,
                )
                continue
            self._store.save_report(_sanitize_report(report))
def _sanitize_report(report: DiagnosisReport) -> DiagnosisReport:
    """Redact secrets before report persistence or public rendering."""
    payload = {
        key: redact_sensitive_data(value)
        for key, value in report.model_dump(mode="python", exclude={"markdown"}).items()
    }
    sanitized = DiagnosisReport.model_validate(payload)
    return sanitized.model_copy(update={"markdown": render_markdown(sanitized)})


_DEFAULT_TRACE_SERVICE = trace_service


def _trace_repository_for_storage(storage_path: Path | None) -> TraceService:
    """Keep explicit stores isolated while preserving injectable test/runtime traces."""
    if trace_service is not _DEFAULT_TRACE_SERVICE:
        return trace_service
    return trace_service if storage_path is None else TraceService(storage_path)


report_generator = ReportGenerator()
