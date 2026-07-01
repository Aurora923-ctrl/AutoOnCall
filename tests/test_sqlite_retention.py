"""Tests for SQLite runtime retention cleanup."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.incident import new_model_id
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.sqlite_store import AIOpsSQLiteStore


def test_sqlite_store_cleanup_older_than_removes_old_runtime_records(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    old_time = datetime.now(UTC) - timedelta(days=30)
    recent_time = datetime.now(UTC)
    old_incident_id = new_model_id("inc-old")
    recent_incident_id = new_model_id("inc-new")

    old_event = TraceEvent(
        incident_id=old_incident_id,
        trace_id="trace-old",
        node_name="workflow",
        event_type="workflow_started",
        created_at=old_time,
    )
    recent_event = TraceEvent(
        incident_id=recent_incident_id,
        trace_id="trace-new",
        node_name="workflow",
        event_type="workflow_started",
        created_at=recent_time,
    )
    old_report = DiagnosisReport(
        incident_id=old_incident_id,
        trace_id="trace-old",
        created_at=old_time,
        markdown="# old",
        root_cause="old",
    )
    recent_report = DiagnosisReport(
        incident_id=recent_incident_id,
        trace_id="trace-new",
        created_at=recent_time,
        markdown="# new",
        root_cause="new",
    )

    store.save_trace_event(old_event)
    store.save_trace_event(recent_event)
    store.save_report(old_report)
    store.save_report(recent_report)

    dry_run = store.cleanup_older_than(keep_days=14, dry_run=True)
    assert dry_run["deleted"]["trace_events"] == 1
    assert dry_run["deleted"]["diagnosis_reports"] == 1
    assert store.get_latest_report(old_incident_id) is not None

    result = store.cleanup_older_than(keep_days=14)
    assert result["deleted"]["trace_events"] == 1
    assert result["deleted"]["diagnosis_reports"] == 1
    assert store.list_trace_events(incident_id=old_incident_id) == []
    assert store.get_latest_report(old_incident_id) is None
    assert store.list_trace_events(incident_id=recent_incident_id)
    assert store.get_latest_report(recent_incident_id) is not None


def test_sqlite_store_cleanup_requires_positive_retention(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")

    with pytest.raises(ValueError, match="keep_days"):
        store.cleanup_older_than(keep_days=0)
