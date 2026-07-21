from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.core import observability
from app.core.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    call_sync_with_resilience,
    call_with_resilience,
    run_bounded_sync_call,
)
from app.services import mysql_store
from app.services.schema_migrations import SchemaMigration, apply_sqlite_migrations

ROOT = Path(__file__).resolve().parents[1]


def test_sqlite_schema_migrations_are_versioned_and_idempotent(tmp_path) -> None:
    database = tmp_path / "migrations.db"
    calls: list[int] = []

    def apply_v1(connection) -> None:
        calls.append(1)
        connection.execute("CREATE TABLE example(id INTEGER PRIMARY KEY)")

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        migrations = [SchemaMigration(1, "create_example", apply_v1)]
        apply_sqlite_migrations(connection, migrations)
        apply_sqlite_migrations(connection, migrations)
        versions = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert calls == [1]
    assert [(row["version"], row["name"]) for row in versions] == [(1, "create_example")]


def test_sqlite_schema_migration_rejects_version_name_drift(tmp_path) -> None:
    database = tmp_path / "migration-drift.db"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        apply_sqlite_migrations(
            connection,
            [SchemaMigration(1, "create_example", lambda _connection: None)],
        )
        with pytest.raises(RuntimeError, match="already recorded"):
            apply_sqlite_migrations(
                connection,
                [SchemaMigration(1, "different_migration", lambda _connection: None)],
            )


@pytest.mark.asyncio
async def test_resilience_retries_within_total_budget() -> None:
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("transient")
        return "ok"

    result = await call_with_resilience(
        "test-retry",
        "invoke",
        flaky,
        timeout_seconds=1,
        max_attempts=2,
        retry_delay_seconds=0,
        is_retryable=lambda exc: isinstance(exc, TimeoutError),
        failure_threshold=3,
        recovery_timeout_seconds=1,
    )

    assert result == "ok"
    assert attempts == 2


def test_circuit_breaker_opens_after_threshold() -> None:
    breaker = CircuitBreaker("test-circuit", failure_threshold=2, recovery_timeout_seconds=60)

    breaker.record_failure()
    breaker.record_failure()

    assert breaker.state == "open"
    assert breaker.allow_request() is False


def test_half_open_circuit_allows_only_one_probe(monkeypatch) -> None:
    breaker = CircuitBreaker("half-open", failure_threshold=1, recovery_timeout_seconds=1)
    breaker.record_failure()
    monkeypatch.setattr(breaker, "_opened_at", time.monotonic() - 2)

    assert breaker.try_acquire_request() is True
    assert breaker.try_acquire_request() is False

    breaker.release_request()
    assert breaker.try_acquire_request() is True


def test_half_open_non_retryable_failure_reopens_circuit(monkeypatch) -> None:
    breaker = CircuitBreaker("half-open-non-retryable", failure_threshold=1, recovery_timeout_seconds=1)
    breaker.record_failure()
    monkeypatch.setattr(breaker, "_opened_at", time.monotonic() - 2)

    assert breaker.try_acquire_request() is True
    breaker.record_non_retryable_failure()

    assert breaker.state == "open"
    assert breaker.try_acquire_request() is False


def test_sync_resilience_enforces_timeout_budget() -> None:
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="timed out"):
        call_sync_with_resilience(
            "sync-timeout",
            "query",
            lambda: time.sleep(0.2),
            timeout_seconds=0.01,
            max_attempts=1,
            retry_delay_seconds=0,
            is_retryable=lambda exc: isinstance(exc, TimeoutError),
            failure_threshold=3,
            recovery_timeout_seconds=1,
        )

    assert time.monotonic() - started < 0.15


@pytest.mark.asyncio
async def test_bounded_sync_call_releases_capacity_after_deadline_expires_before_await() -> None:
    release = threading.Event()

    with pytest.raises(TimeoutError, match="timed out"):
        await run_bounded_sync_call(
            "bounded-sync-timeout",
            "query",
            lambda: release.wait(0.2),
            timeout_seconds=0.01,
        )

    release.set()
    result = await run_bounded_sync_call(
        "bounded-sync-recovery",
        "query",
        lambda: "ok",
        timeout_seconds=1,
    )

    assert result == "ok"


@pytest.mark.asyncio
async def test_open_circuit_fails_before_call(monkeypatch) -> None:
    called = False

    async def should_not_run() -> None:
        nonlocal called
        called = True

    from app.core import resilience

    breaker = CircuitBreaker("forced-open", failure_threshold=1, recovery_timeout_seconds=60)
    breaker.record_failure()
    monkeypatch.setitem(resilience._breakers, "forced-open", breaker)

    with pytest.raises(CircuitOpenError):
        await call_with_resilience(
            "forced-open",
            "invoke",
            should_not_run,
            timeout_seconds=1,
            max_attempts=1,
            retry_delay_seconds=0,
            is_retryable=lambda _exc: False,
            failure_threshold=1,
            recovery_timeout_seconds=60,
        )

    assert called is False


def test_dependency_operation_preserves_explicit_status(monkeypatch) -> None:
    observations: list[tuple[str, dict[str, object]]] = []

    class Counter:
        def add(self, value, labels) -> None:
            observations.append(("counter", dict(labels)))

    class Histogram:
        def record(self, value, labels) -> None:
            observations.append(("histogram", dict(labels)))

    monkeypatch.setattr(observability, "_initialized", True)
    monkeypatch.setattr(observability, "_dependency_calls", Counter())
    monkeypatch.setattr(observability, "_dependency_duration", Histogram())
    monkeypatch.setattr(observability, "_circuit_rejections", Counter())

    with pytest.raises(CircuitOpenError):
        with observability.dependency_operation("test", "invoke") as observation:
            observation.status = "circuit_open"
            raise CircuitOpenError("open")

    assert any(labels.get("status") == "circuit_open" for _, labels in observations)


def test_mysql_pool_is_shared_and_closed_once(monkeypatch) -> None:
    created: list[object] = []
    closed: list[object] = []

    class Pool:
        def connection(self):
            return object()

        def close(self) -> None:
            closed.append(self)

    def pool_factory(**_kwargs):
        pool = Pool()
        created.append(pool)
        return pool

    import dbutils.pooled_db

    monkeypatch.setattr(dbutils.pooled_db, "PooledDB", pool_factory)
    mysql_store.close_mysql_pools()
    first = object.__new__(mysql_store.AIOpsMySQLStore)
    second = object.__new__(mysql_store.AIOpsMySQLStore)
    settings = {
        "host": "localhost",
        "port": 3306,
        "user": "user",
        "password": "password",
        "database": "autooncall",
        "charset": "utf8mb4",
        "connect_timeout": 1,
        "read_timeout": 1,
        "write_timeout": 1,
    }
    first.connection_settings = dict(settings)
    second.connection_settings = dict(settings)

    first._connect()
    second._connect()
    mysql_store.close_mysql_pools()

    assert len(created) == 1
    assert closed == created


def test_mysql_schema_migration_runs_compatibility_upgrade() -> None:
    store = object.__new__(mysql_store.AIOpsMySQLStore)
    calls: list[str] = []
    store._ensure_approval_idempotency_columns = lambda _cursor: calls.append("approval")
    store._ensure_change_execution_scope_unique_index = lambda _cursor: calls.append("change")

    store._apply_mysql_approval_and_change_idempotency(object())

    assert calls == ["approval", "change"]


def test_async_blocking_audit_detects_path_read_text(tmp_path, monkeypatch) -> None:
    from scripts.maintenance import audit_async_blocking
    from scripts.maintenance.audit_async_blocking import audit_file

    source = tmp_path / "blocking.py"
    source.write_text(
        "from pathlib import Path\n"
        "async def read_file():\n"
        "    return Path('payload.json').read_text()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(audit_async_blocking, "ROOT", tmp_path)

    findings = audit_file(source)

    assert findings and findings[0].endswith("Path.read_text")


def test_quality_gate_separates_live_model_evaluations() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    verify = makefile.split("\nverify:", maxsplit=1)[1].split("\ncheck-all:", maxsplit=1)[0]
    live_eval = makefile.split("\nlive-eval:", maxsplit=1)[1].split(
        "\npre-commit-install:", maxsplit=1
    )[0]

    assert "performance-real-model" not in verify
    assert "eval-ragas-full-runtime-core" not in verify
    assert "performance-real-model" in live_eval
    assert "eval-ragas-full-runtime-core" in live_eval
