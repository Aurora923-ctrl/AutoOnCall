from pathlib import Path

from app.services.legacy_migration import resolve_legacy_jsonl_path


def test_resolve_legacy_jsonl_path_defaults_to_logs_file() -> None:
    assert resolve_legacy_jsonl_path(None, "traces.jsonl") == Path("logs/traces.jsonl")


def test_resolve_legacy_jsonl_path_preserves_explicit_jsonl_file(tmp_path) -> None:
    legacy_file = tmp_path / "approvals.jsonl"

    assert resolve_legacy_jsonl_path(legacy_file, "approvals.jsonl") == legacy_file


def test_resolve_legacy_jsonl_path_ignores_sqlite_database(tmp_path) -> None:
    database_file = tmp_path / "aiops.db"

    assert resolve_legacy_jsonl_path(database_file, "reports.jsonl") is None
