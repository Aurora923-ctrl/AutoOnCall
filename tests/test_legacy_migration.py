import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.a2a import A2ATaskRecord
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
from scripts.maintenance.migrate_aiops_sqlite_to_mysql import _read_models


def test_resolve_legacy_jsonl_path_defaults_to_logs_file() -> None:
    assert resolve_legacy_jsonl_path(None, "traces.jsonl") == Path("logs/traces.jsonl")


def test_resolve_legacy_jsonl_path_preserves_explicit_jsonl_file(tmp_path) -> None:
    legacy_file = tmp_path / "approvals.jsonl"

    assert resolve_legacy_jsonl_path(legacy_file, "approvals.jsonl") == legacy_file


def test_resolve_legacy_jsonl_path_ignores_sqlite_database(tmp_path) -> None:
    database_file = tmp_path / "aiops.db"

    assert resolve_legacy_jsonl_path(database_file, "reports.jsonl") is None


def test_mysql_latest_report_list_uses_same_ordering_as_single_lookup() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.query = ""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            self.query = query

        def fetchall(self):
            return []

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_instance = FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.cursor_instance

    connection = FakeConnection()
    store = object.__new__(AIOpsMySQLStore)
    store._connect = lambda: connection

    assert store.list_latest_reports() == []
    normalized = " ".join(connection.cursor_instance.query.split())
    assert "newer.created_at > current.created_at" in normalized
    assert "newer.created_at = current.created_at" in normalized
    assert "newer.id > current.id" in normalized


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
    store.create_a2a_task_record(
        A2ATaskRecord(
            task_id="task-1",
            message_id="msg-1",
            request_fingerprint="f" * 64,
            owner_id="principal-1",
            skill="diagnose_incident",
            incident_id="inc-1",
            state="TASK_STATE_SUBMITTED",
        )
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
            execute=False,
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
        "a2a_tasks": 1,
        "incident_states": 1,
        "diagnosis_reports": 1,
    }


def test_mysql_store_scope_index_migration_rejects_duplicate_groups() -> None:
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

    cursor = FakeCursor()

    with pytest.raises(RuntimeError, match="business-scope idempotency"):
        AIOpsMySQLStore._ensure_change_execution_scope_unique_index(
            object.__new__(AIOpsMySQLStore),
            cursor,
        )

    assert not any("ALTER TABLE change_executions" in statement for statement in cursor.statements)


def test_mysql_store_fails_closed_when_approval_idempotency_migration_fails() -> None:
    class FakeCursor:
        def execute(self, statement: str, params=None) -> None:
            if "information_schema.columns" not in statement:
                raise PermissionError("ALTER denied")

        def fetchall(self):
            return []

    with pytest.raises(RuntimeError, match="approval schema is incompatible"):
        AIOpsMySQLStore._ensure_approval_idempotency_columns(
            object.__new__(AIOpsMySQLStore),
            FakeCursor(),
        )


def test_mysql_runtime_schema_capacity_matches_model_contracts() -> None:
    runtime_schema = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "adapters"
        / "mysql-init"
        / "002_aiops_runtime_store.sql"
    ).read_text(encoding="utf-8")

    assert "action VARCHAR(1000) NOT NULL" in runtime_schema
    assert runtime_schema.count("status VARCHAR(64) NOT NULL") >= 3
    assert "environment VARCHAR(80) NOT NULL" in runtime_schema
    assert "approval_status VARCHAR(64) NOT NULL" in runtime_schema


def test_sqlite_a2a_owner_migration_backfills_existing_payload(tmp_path) -> None:
    database_file = tmp_path / "legacy-a2a.db"
    with sqlite3.connect(database_file) as connection:
        connection.executescript(
            """
            CREATE TABLE a2a_tasks (
                task_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                skill TEXT NOT NULL,
                incident_id TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            """
        )
        record = A2ATaskRecord(
            task_id="task-legacy-owner",
            message_id="msg-legacy-owner",
            request_fingerprint="c" * 64,
            owner_id="principal-legacy",
            skill="diagnose_incident",
            incident_id="inc-legacy-owner",
            state="TASK_STATE_COMPLETED",
        )
        connection.execute(
            """
            INSERT INTO a2a_tasks (
                task_id, message_id, request_fingerprint, skill, incident_id,
                state, created_at, updated_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.task_id,
                record.message_id,
                record.request_fingerprint,
                record.skill,
                record.incident_id,
                record.state,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
                record.model_dump_json(),
            ),
        )

    store = AIOpsSQLiteStore(database_file)

    assert [item.task_id for item in store.list_a2a_task_records(owner_id="principal-legacy")] == [
        "task-legacy-owner"
    ]


def test_mysql_store_expands_legacy_runtime_column_capacities() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str, _params=None) -> None:
            self.statements.append(" ".join(statement.split()))

        def fetchall(self):
            return [
                {
                    "table_name": "approval_requests",
                    "column_name": "action",
                    "character_maximum_length": 512,
                },
                {
                    "table_name": "aiops_sessions",
                    "column_name": "status",
                    "character_maximum_length": 32,
                },
                {
                    "table_name": "incident_states",
                    "column_name": "status",
                    "character_maximum_length": 32,
                },
                {
                    "table_name": "incident_states",
                    "column_name": "environment",
                    "character_maximum_length": 64,
                },
                {
                    "table_name": "incident_states",
                    "column_name": "approval_status",
                    "character_maximum_length": 32,
                },
            ]

    cursor = FakeCursor()

    AIOpsMySQLStore._ensure_runtime_column_capacities(cursor)

    assert "ALTER TABLE approval_requests MODIFY action VARCHAR(1000) NOT NULL" in cursor.statements
    assert "ALTER TABLE aiops_sessions MODIFY status VARCHAR(64) NOT NULL" in cursor.statements
    assert "ALTER TABLE incident_states MODIFY status VARCHAR(64) NOT NULL" in cursor.statements
    assert (
        "ALTER TABLE incident_states MODIFY environment VARCHAR(80) NOT NULL" in cursor.statements
    )
    assert (
        "ALTER TABLE incident_states MODIFY approval_status VARCHAR(64) NOT NULL"
        in cursor.statements
    )


def test_mysql_store_rejects_non_transactional_runtime_tables() -> None:
    class FakeCursor:
        def execute(self, _statement: str, _params=None) -> None:
            return None

        def fetchall(self):
            tables = [
                "alert_events",
                "trace_events",
                "approval_requests",
                "change_executions",
                "aiops_sessions",
                "incident_states",
                "diagnosis_reports",
            ]
            return [
                {"table_name": table, "engine": "MyISAM" if table == "trace_events" else "InnoDB"}
                for table in tables
            ]

    with pytest.raises(RuntimeError, match="non_innodb=trace_events"):
        AIOpsMySQLStore._require_transactional_runtime_tables(FakeCursor())


def test_sqlite_migration_rejects_non_object_payload_with_row_context(tmp_path) -> None:
    database_file = tmp_path / "invalid-source.db"
    store = AIOpsSQLiteStore(database_file)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            INSERT INTO trace_events (
                event_id, trace_id, incident_id, event_type, node_name,
                step_id, tool_name, status, created_at, payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt-invalid",
                "trace-invalid",
                "inc-invalid",
                "node",
                "planner",
                None,
                None,
                "success",
                "2026-07-18T00:00:00+00:00",
                "[]",
            ),
        )

    with sqlite3.connect(store.database_path) as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(RuntimeError, match=r"trace_events rowid=\d+"):
            _read_models(connection, "trace_events", TraceEvent)


def test_sqlite_to_mysql_migration_uses_one_atomic_non_overwriting_import(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    database_file = tmp_path / "aiops.db"
    store = AIOpsSQLiteStore(database_file)
    store.save_alert_event(
        AlertEvent(fingerprint="fp-atomic", incident_id="inc-atomic", service_name="order-service")
    )
    store.save_aiops_session_snapshot(
        AIOpsSessionSnapshot(
            session_id="sess-atomic",
            incident_id="inc-atomic",
            trace_id="trace-atomic",
        )
    )

    captured: dict[str, list[object]] = {}

    class FakeMySQLStore:
        def __init__(self, dsn: str) -> None:
            assert dsn.endswith("/autooncall")

        def import_runtime_state(self, **records):
            captured.update(records)
            return {table: len(items) for table, items in records.items()}

    monkeypatch.setattr(migrate_aiops_sqlite_to_mysql, "AIOpsMySQLStore", FakeMySQLStore)
    monkeypatch.setattr(
        migrate_aiops_sqlite_to_mysql,
        "parse_args",
        lambda: SimpleNamespace(
            sqlite=str(database_file),
            mysql_dsn="mysql+pymysql://user:password@localhost:3306/autooncall",
            dry_run=False,
            execute=True,
        ),
    )

    assert migrate_aiops_sqlite_to_mysql.main() == 0

    summary = json.loads(capsys.readouterr().out)
    assert len(captured["alert_events"]) == 1
    assert len(captured["aiops_sessions"]) == 1
    assert summary["imported"] == summary["counts"]
    assert all(count == 0 for count in summary["skipped_existing"].values())


def test_mysql_runtime_import_rolls_back_all_tables_on_partial_failure() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.execute_count = 0
            self.rows: list[dict[str, object] | None] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, _statement: str, _params=None) -> None:
            self.execute_count += 1
            if self.execute_count == 2:
                raise RuntimeError("simulated insert failure")
            self.rows.append(None)

        def fetchone(self):
            return self.rows.pop(0)

        @property
        def rowcount(self) -> int:
            return 1

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_value = FakeCursor()
            self.began = False
            self.committed = False
            self.rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def cursor(self):
            return self.cursor_value

        def begin(self) -> None:
            self.began = True

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

    connection = FakeConnection()
    mysql_store = object.__new__(AIOpsMySQLStore)
    mysql_store._connect = lambda: connection

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        mysql_store.import_runtime_state(
            alert_events=[
                AlertEvent(
                    fingerprint="fp-rollback",
                    incident_id="inc-rollback",
                    service_name="order-service",
                )
            ],
            trace_events=[
                TraceEvent(
                    event_id="evt-rollback",
                    trace_id="trace-rollback",
                    incident_id="inc-rollback",
                    node_name="planner",
                )
            ],
            approval_requests=[],
            change_executions=[],
            aiops_sessions=[],
            a2a_tasks=[],
            incident_states=[],
            diagnosis_reports=[],
        )

    assert connection.began is True
    assert connection.committed is False
    assert connection.rolled_back is True


@pytest.mark.parametrize("operation", ["cleanup", "reset"])
def test_mysql_destructive_maintenance_rolls_back_on_partial_failure(operation: str) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.execute_count = 0
            self.last_statement = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, statement: str, _params=None) -> None:
            self.execute_count += 1
            self.last_statement = statement
            if self.execute_count == 4:
                raise RuntimeError("simulated maintenance failure")

        def fetchone(self):
            return {"count": 1}

        def fetchall(self):
            return [{"incident_id": "inc-maintenance"}]

        @property
        def rowcount(self) -> int:
            return 1

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_value = FakeCursor()
            self.began = False
            self.committed = False
            self.rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def cursor(self):
            return self.cursor_value

        def begin(self) -> None:
            self.began = True

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

    connection = FakeConnection()
    mysql_store = object.__new__(AIOpsMySQLStore)
    mysql_store.storage_path = "mysql+pymysql://user:***@localhost/autooncall"
    mysql_store._connect = lambda: connection

    with pytest.raises(RuntimeError, match="simulated maintenance failure"):
        if operation == "cleanup":
            mysql_store.cleanup_older_than(keep_days=14)
        else:
            mysql_store.reset_runtime_data()

    assert connection.began is True
    assert connection.committed is False
    assert connection.rolled_back is True
