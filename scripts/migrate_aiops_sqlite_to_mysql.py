"""Migrate AIOps runtime state from SQLite to the configured MySQL store."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import config
from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.mysql_store import AIOpsMySQLStore


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Migrate AIOps runtime state from SQLite to MySQL.",
    )
    parser.add_argument("--sqlite", default=config.aiops_sqlite_path)
    parser.add_argument("--mysql-dsn", default=config.resolved_mysql_dsn)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    """CLI entry point."""
    args = parse_args()
    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite database not found: {sqlite_path}")
    if not args.mysql_dsn:
        raise SystemExit("MySQL DSN is required via --mysql-dsn, MYSQL_DSN, or MYSQL_HOST fields")

    traces = _read_models(sqlite_path, "trace_events", TraceEvent)
    approvals = _read_models(sqlite_path, "approval_requests", ApprovalRequest)
    reports = _read_models(sqlite_path, "diagnosis_reports", DiagnosisReport)
    change_executions = _read_models(sqlite_path, "change_executions", ChangeExecution)

    summary = {
        "sqlite": str(sqlite_path),
        "mysql": _redact_dsn(args.mysql_dsn),
        "dry_run": args.dry_run,
        "counts": {
            "trace_events": len(traces),
            "approval_requests": len(approvals),
            "diagnosis_reports": len(reports),
            "change_executions": len(change_executions),
        },
    }

    if not args.dry_run:
        store = AIOpsMySQLStore(args.mysql_dsn)
        for event in traces:
            store.save_trace_event(event)
        for approval in approvals:
            store.save_approval_request(approval)
        for report in reports:
            store.save_report(report)
        for execution in change_executions:
            store.save_change_execution(execution)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _read_models(sqlite_path: Path, table: str, model_type: type) -> list[Any]:
    with sqlite3.connect(sqlite_path) as connection:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(f"SELECT payload FROM {table} ORDER BY rowid ASC").fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
    models = []
    for row in rows:
        payload = json.loads(str(row["payload"]))
        if isinstance(payload, dict):
            models.append(model_type.model_validate(payload))
    return models


def _redact_dsn(dsn: str) -> str:
    if "@" not in dsn or ":" not in dsn.split("@", 1)[0]:
        return dsn
    scheme, rest = dsn.split("://", 1)
    _auth, host = rest.split("@", 1)
    username = _auth.split(":", 1)[0]
    return f"{scheme}://{username}:***@{host}"


if __name__ == "__main__":
    raise SystemExit(main())
