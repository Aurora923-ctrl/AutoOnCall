"""Shared behavioral contract tests for SQLite and MySQL AIOps stores."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.report import DiagnosisReport
from app.services.mysql_store import AIOpsMySQLStore
from app.services.sqlite_store import AIOpsSQLiteStore


class FakeMySQLCursor:
    """Small stateful boundary fake for the store methods exercised below."""

    def __init__(self, database: FakeMySQLDatabase) -> None:
        self.database = database
        self.rowcount = 0
        self._one: dict[str, Any] | None = None
        self._many: list[dict[str, Any]] = []

    def __enter__(self) -> FakeMySQLCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, params: Any = None) -> None:
        sql = " ".join(statement.split()).lower()
        values = tuple(params or ())
        self.rowcount = 0
        self._one = None
        self._many = []

        if sql.startswith("insert into diagnosis_reports"):
            report_id, incident_id, trace_id, created_at, updated_at, payload = values
            existing = self.database.reports.get(str(report_id))
            row_id = existing["id"] if existing else self.database.next_id()
            self.database.reports[str(report_id)] = {
                "id": row_id,
                "incident_id": str(incident_id),
                "trace_id": str(trace_id),
                "created_at": str(created_at),
                "updated_at": str(updated_at),
                "payload": str(payload),
            }
            self.rowcount = 1
            return

        if sql.startswith("select payload from diagnosis_reports where report_id"):
            row = self.database.reports.get(str(values[0]))
            self._one = {"payload": row["payload"]} if row else None
            return

        if "from diagnosis_reports" in sql and "where incident_id = %s" in sql:
            incident_id = str(values[0])
            rows = [
                row
                for row in self.database.reports.values()
                if row["incident_id"] == incident_id
            ]
            rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
            self._one = {"payload": rows[0]["payload"]} if rows else None
            return

        if "select current.payload" in sql and "from diagnosis_reports as current" in sql:
            latest: dict[str, dict[str, Any]] = {}
            for row in self.database.reports.values():
                current = latest.get(row["incident_id"])
                if current is None or (row["created_at"], row["id"]) > (
                    current["created_at"],
                    current["id"],
                ):
                    latest[row["incident_id"]] = row
            rows = sorted(
                latest.values(),
                key=lambda row: (row["created_at"], row["id"]),
                reverse=True,
            )
            self._many = [{"payload": row["payload"]} for row in rows]
            return

        if sql.startswith("insert ignore into aiops_sessions"):
            session_id = str(values[0])
            if session_id in self.database.sessions:
                return
            self.database.sessions[session_id] = {
                "id": self.database.next_id(),
                "incident_id": str(values[1]),
                "trace_id": str(values[2]),
                "status": str(values[3]),
                "node_name": str(values[4]),
                "created_at": str(values[5]),
                "updated_at": str(values[6]),
                "payload": str(values[7]),
            }
            self.rowcount = 1
            return

        if sql.startswith("select payload from aiops_sessions where session_id"):
            row = self.database.sessions.get(str(values[0]))
            self._one = {"payload": row["payload"]} if row else None
            return

        if sql.startswith("update aiops_sessions set"):
            session_id = str(values[6])
            expected_statuses = {str(value) for value in values[7:]}
            row = self.database.sessions.get(session_id)
            if row is None or row["status"] not in expected_statuses:
                return
            row.update(
                {
                    "incident_id": str(values[0]),
                    "trace_id": str(values[1]),
                    "status": str(values[2]),
                    "node_name": str(values[3]),
                    "updated_at": str(values[4]),
                    "payload": str(values[5]),
                }
            )
            self.rowcount = 1
            return

        raise AssertionError(f"Unexpected MySQL statement in contract fake: {statement}")

    def fetchone(self) -> dict[str, Any] | None:
        return self._one

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._many)


class FakeMySQLConnection:
    def __init__(self, database: FakeMySQLDatabase) -> None:
        self.database = database
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> FakeMySQLConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> FakeMySQLCursor:
        return FakeMySQLCursor(self.database)

    def begin(self) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class FakeMySQLDatabase:
    def __init__(self) -> None:
        self.reports: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self._next_id = 0

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def connect(self) -> FakeMySQLConnection:
        return FakeMySQLConnection(self)


@pytest.fixture(params=["sqlite", "mysql"])
def aiops_store(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "sqlite":
        return AIOpsSQLiteStore(tmp_path / "contract.db")

    return _mysql_store()


def _mysql_store() -> AIOpsMySQLStore:
    database = FakeMySQLDatabase()
    store = object.__new__(AIOpsMySQLStore)
    store.storage_path = "mysql+pymysql://contract:***@fake/autooncall"
    store._connect = database.connect
    return store


def test_store_contract_persists_reports_and_returns_latest_per_incident(aiops_store: Any) -> None:
    base_time = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    first = DiagnosisReport(
        report_id="report-first",
        incident_id="incident-shared",
        trace_id="trace-first",
        summary="first",
        created_at=base_time,
    )
    latest = DiagnosisReport(
        report_id="report-latest",
        incident_id="incident-shared",
        trace_id="trace-latest",
        summary="latest",
        created_at=base_time + timedelta(seconds=1),
    )
    other = DiagnosisReport(
        report_id="report-other",
        incident_id="incident-other",
        trace_id="trace-other",
        summary="other",
        created_at=base_time + timedelta(seconds=2),
    )

    for report in (first, latest, other):
        aiops_store.save_report(report)

    assert aiops_store.get_report(first.report_id).model_dump(mode="json") == first.model_dump(
        mode="json"
    )
    assert aiops_store.get_latest_report("incident-shared").report_id == latest.report_id
    assert {report.report_id for report in aiops_store.list_latest_reports()} == {
        latest.report_id,
        other.report_id,
    }


def test_store_contract_session_create_and_compare_and_set_are_consistent(
    aiops_store: Any,
) -> None:
    initial = AIOpsSessionSnapshot(
        session_id="session-contract",
        incident_id="incident-contract",
        trace_id="trace-contract",
        status="running",
        response="",
    )

    assert aiops_store.create_aiops_session_snapshot(initial) is True
    assert aiops_store.create_aiops_session_snapshot(initial.model_copy()) is False

    completed = initial.model_copy(update={"status": "completed", "response": "# complete"})
    assert (
        aiops_store.update_aiops_session_snapshot_if_status(
            completed,
            expected_statuses={"running"},
        )
        is True
    )
    stored = aiops_store.get_aiops_session_snapshot(initial.session_id)
    assert stored.status == "completed"
    assert stored.response == "# complete"
    assert stored.created_at == initial.created_at

    stale_update = completed.model_copy(update={"status": "failed"})
    assert (
        aiops_store.update_aiops_session_snapshot_if_status(
            stale_update,
            expected_statuses={"running"},
        )
        is False
    )
    assert aiops_store.get_aiops_session_snapshot(initial.session_id).status == "completed"


def test_mysql_contract_fake_stores_valid_json_payloads() -> None:
    aiops_store = _mysql_store()
    database = aiops_store._connect().database
    report = DiagnosisReport(report_id="json-report", incident_id="json-incident")
    aiops_store.save_report(report)

    assert json.loads(database.reports[report.report_id]["payload"])["report_id"] == report.report_id
