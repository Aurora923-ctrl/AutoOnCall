"""Migrate AIOps runtime state from SQLite to the configured MySQL store."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import config
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.alert import AlertEvent
from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.mysql_store import AIOpsMySQLStore

ModelT = TypeVar("ModelT", bound=BaseModel)
RUNTIME_TABLES = (
    "alert_events",
    "trace_events",
    "approval_requests",
    "change_executions",
    "aiops_sessions",
    "incident_states",
    "diagnosis_reports",
)
RUNTIME_MODEL_TABLES = frozenset(RUNTIME_TABLES) - {"approval_requests"}


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Migrate AIOps runtime state from SQLite to MySQL.",
    )
    parser.add_argument("--sqlite", default=config.aiops_sqlite_path)
    parser.add_argument("--mysql-dsn", default=config.resolved_mysql_dsn)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually import records. Without this flag the command is a dry run.",
    )
    return parser.parse_args()


def main() -> int:
    """CLI entry point."""
    args = parse_args()
    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite database not found: {sqlite_path}")
    if not args.mysql_dsn:
        raise SystemExit("MySQL DSN is required via --mysql-dsn, MYSQL_DSN, or MYSQL_HOST fields")

    with _open_readonly_snapshot(sqlite_path) as source:
        _require_runtime_tables(source)
        alert_events = _read_models(source, "alert_events", AlertEvent)
        traces = _read_models(source, "trace_events", TraceEvent)
        approvals = _read_approval_models(source)
        change_executions = _read_models(source, "change_executions", ChangeExecution)
        aiops_sessions = _read_models(source, "aiops_sessions", AIOpsSessionSnapshot)
        incident_states = _read_models(source, "incident_states", IncidentState)
        reports = _read_models(source, "diagnosis_reports", DiagnosisReport)

    dry_run = bool(args.dry_run or not args.execute)
    summary: dict[str, Any] = {
        "sqlite": str(sqlite_path),
        "mysql": _redact_dsn(args.mysql_dsn),
        "dry_run": dry_run,
        "counts": {
            "alert_events": len(alert_events),
            "trace_events": len(traces),
            "approval_requests": len(approvals),
            "change_executions": len(change_executions),
            "aiops_sessions": len(aiops_sessions),
            "incident_states": len(incident_states),
            "diagnosis_reports": len(reports),
        },
    }

    if not dry_run:
        store = AIOpsMySQLStore(args.mysql_dsn)
        imported = store.import_runtime_state(
            alert_events=alert_events,
            trace_events=traces,
            approval_requests=approvals,
            change_executions=change_executions,
            aiops_sessions=aiops_sessions,
            incident_states=incident_states,
            diagnosis_reports=reports,
        )
        conflicts = imported.pop("conflicts", {})
        summary["imported"] = imported
        summary["skipped_existing"] = {
            table: summary["counts"][table] - imported[table] for table in summary["counts"]
        }
        summary["conflicts"] = conflicts
        if any(int(count or 0) for count in conflicts.values()):
            raise RuntimeError(
                "Migration found existing MySQL rows with different payloads; "
                "no rows were imported. Resolve conflicts before retrying."
            )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


@contextmanager
def _open_readonly_snapshot(sqlite_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def _read_models(
    connection: sqlite3.Connection,
    table: str,
    model_type: type[ModelT],
) -> list[ModelT]:
    table_name = _runtime_table_name(table, allowed=RUNTIME_MODEL_TABLES)
    rows = connection.execute(
        # SQLite cannot bind identifiers; table_name is returned only from the
        # module-owned runtime table whitelist.
        f"SELECT rowid AS source_rowid, payload FROM {table_name} ORDER BY rowid ASC"  # nosec B608
    ).fetchall()
    models: list[ModelT] = []
    for row in rows:
        source_rowid = int(row["source_rowid"])
        try:
            payload = json.loads(str(row["payload"]))
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            models.append(model_type.model_validate(payload))
        except Exception as exc:
            raise RuntimeError(f"Invalid SQLite payload in {table} rowid={source_rowid}") from exc
    return models


def _runtime_table_name(table: str, *, allowed: frozenset[str]) -> str:
    """Return a runtime table identifier only when it belongs to an explicit whitelist."""
    if table not in allowed:
        raise ValueError(f"Unsupported AIOps runtime table: {table}")
    return table


def _require_runtime_tables(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    available = {str(row["name"]) for row in rows}
    missing = sorted(set(RUNTIME_TABLES) - available)
    if missing:
        raise RuntimeError(
            "SQLite runtime schema is incomplete; missing tables: " + ", ".join(missing)
        )


def _read_approval_models(
    connection: sqlite3.Connection,
) -> list[tuple[ApprovalRequest, str | None]]:
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(approval_requests)").fetchall()
    }
    idempotency_column = (
        _approval_idempotency_projection(columns)
        if "idempotency_key" in columns
        else "NULL AS idempotency_key"
    )
    rows = connection.execute(
        # The projection is selected from a fixed schema-derived whitelist and
        # never contains user-controlled text.
        f"""
        SELECT rowid AS source_rowid, payload, {idempotency_column}
        FROM approval_requests
        ORDER BY rowid ASC
        """  # nosec B608
    ).fetchall()
    approvals: list[tuple[ApprovalRequest, str | None]] = []
    for row in rows:
        source_rowid = int(row["source_rowid"])
        try:
            payload = json.loads(str(row["payload"]))
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            request = ApprovalRequest.model_validate(payload)
        except Exception as exc:
            raise RuntimeError(
                f"Invalid SQLite payload in approval_requests rowid={source_rowid}"
            ) from exc
        idempotency_key = str(row["idempotency_key"] or "").strip() or None
        if idempotency_key is None:
            idempotency_key = str(request.metadata.get("idempotency_key") or "").strip() or None
        approvals.append((request, idempotency_key))
    return approvals


def _approval_idempotency_projection(columns: set[str]) -> str:
    """Return the optional approval projection from the known SQLite schema."""
    if "idempotency_key" not in columns:
        raise ValueError("approval_requests.idempotency_key is not available")
    return "idempotency_key AS idempotency_key"


def _redact_dsn(dsn: str) -> str:
    if "@" not in dsn or ":" not in dsn.split("@", 1)[0]:
        return dsn
    scheme, rest = dsn.split("://", 1)
    _auth, host = rest.split("@", 1)
    username = _auth.split(":", 1)[0]
    return f"{scheme}://{username}:***@{host}"


if __name__ == "__main__":
    raise SystemExit(main())
