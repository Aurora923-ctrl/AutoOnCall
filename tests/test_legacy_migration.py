import json
from pathlib import Path
from types import SimpleNamespace

from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.alert import AlertEvent
from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.legacy_migration import resolve_legacy_jsonl_path
from app.services.mysql_store import AIOpsMySQLStore
from app.services.sqlite_store import AIOpsSQLiteStore
from scripts import migrate_aiops_sqlite_to_mysql


def test_resolve_legacy_jsonl_path_defaults_to_logs_file() -> None:
    assert resolve_legacy_jsonl_path(None, "traces.jsonl") == Path("logs/traces.jsonl")


def test_resolve_legacy_jsonl_path_preserves_explicit_jsonl_file(tmp_path) -> None:
    legacy_file = tmp_path / "approvals.jsonl"

    assert resolve_legacy_jsonl_path(legacy_file, "approvals.jsonl") == legacy_file


def test_resolve_legacy_jsonl_path_ignores_sqlite_database(tmp_path) -> None:
    database_file = tmp_path / "aiops.db"

    assert resolve_legacy_jsonl_path(database_file, "reports.jsonl") is None


def test_sqlite_to_mysql_migration_dry_run_counts_all_runtime_tables(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    database_file = tmp_path / "aiops.db"
    store = AIOpsSQLiteStore(database_file)
    store.save_alert_event(
        AlertEvent(fingerprint="fp-1", incident_id="inc-1", service_name="order-service")
    )
    store.save_trace_event(TraceEvent(trace_id="trace-1", incident_id="inc-1", node_name="planner"))
    store.save_approval_request(
        ApprovalRequest(incident_id="inc-1", action="restart service", risk_level="high")
    )
    store.save_change_execution(
        ChangeExecution(change_plan_id="plan-1", approval_id="apr-1", incident_id="inc-1")
    )
    store.save_aiops_session_snapshot(
        AIOpsSessionSnapshot(session_id="sess-1", incident_id="inc-1", trace_id="trace-1")
    )
    store.save_incident_state(IncidentState(incident_id="inc-1", status="completed"))
    store.save_report(DiagnosisReport(incident_id="inc-1", trace_id="trace-1"))
    monkeypatch.setattr(
        migrate_aiops_sqlite_to_mysql,
        "parse_args",
        lambda: SimpleNamespace(
            sqlite=str(database_file),
            mysql_dsn="mysql+pymysql://user:password@localhost:3306/autooncall",
            dry_run=True,
        ),
    )

    assert migrate_aiops_sqlite_to_mysql.main() == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["counts"] == {
        "alert_events": 1,
        "trace_events": 1,
        "approval_requests": 1,
        "change_executions": 1,
        "aiops_sessions": 1,
        "incident_states": 1,
        "diagnosis_reports": 1,
    }


def test_mysql_store_scope_index_migration_warns_when_duplicate_groups_exist() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str, params=None) -> None:
            self.statements.append(statement)

        def fetchone(self):
            last = self.statements[-1]
            if "information_schema.statistics" in last:
                return {"index_count": 0}
            if "duplicate_groups" in last:
                return {"duplicate_groups": 2}
            return None

    store = object.__new__(AIOpsMySQLStore)
    store.migration_warnings = []
    cursor = FakeCursor()

    store._ensure_change_execution_scope_unique_index(cursor)

    assert store.migration_warnings
    assert "历史重复安全变更记录" in store.migration_warnings[0]
    assert not any("ALTER TABLE change_executions" in statement for statement in cursor.statements)
