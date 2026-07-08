"""Idempotently seed live Redis/MySQL incident evidence into running Docker containers."""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SQL_PATH = ROOT / "deploy" / "adapters" / "mysql-init" / "001_init.sql"
DEFAULT_REDIS_CONTAINER = "autooncall-full-redis"
DEFAULT_MYSQL_CONTAINER = "autooncall-full-mysql"
MYSQL_USER = "root"
MYSQL_PASSWORD = "123456"
MYSQL_DATABASE = "autooncall"


def run_command(args: list[str], *, input_text: str | None = None) -> str:
    completed = subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def run_best_effort(args: list[str], *, input_text: str | None = None) -> str:
    completed = subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    return (completed.stdout or completed.stderr).strip()


def wait_for_command(
    args: list[str],
    *,
    expected_text: str,
    timeout_seconds: int = 90,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_output = ""
    while time.monotonic() < deadline:
        last_output = run_best_effort(args)
        if expected_text in last_output:
            return
        time.sleep(2)
    raise RuntimeError(
        f"Timed out waiting for command readiness: {' '.join(args)}; last_output={last_output}"
    )


def wait_for_redis(container: str) -> None:
    wait_for_command(
        ["docker", "exec", container, "redis-cli", "ping"],
        expected_text="PONG",
        timeout_seconds=90,
    )


def wait_for_mysql(container: str) -> None:
    wait_for_command(
        [
            "docker",
            "exec",
            container,
            "mysqladmin",
            "ping",
            "-h",
            "127.0.0.1",
            f"-u{MYSQL_USER}",
            f"-p{MYSQL_PASSWORD}",
        ],
        expected_text="mysqld is alive",
        timeout_seconds=120,
    )


def seed_redis(container: str) -> None:
    wait_for_redis(container)
    run_command(
        [
            "docker",
            "exec",
            container,
            "redis-cli",
            "HSET",
            "autooncall:incident:order-service:redis-maxclients",
            "service",
            "order-service",
            "severity",
            "P1",
            "symptom",
            "Redis connection timeout and 5xx spike",
            "dependency",
            "redis-cluster-prod",
            "connected_clients",
            "9940",
            "maxclients",
            "10000",
            "blocked_clients",
            "37",
            "slowlog_len",
            "40",
            "used_memory_human",
            "12.4G",
            "maxmemory_human",
            "16G",
            "root_cause",
            "Redis connected_clients reached maxclients and application connection pool timed out",
            "approval_required",
            "true",
            "source",
            "live-redis-seed",
            "updated_at",
            "2026-07-06T10:00:00Z",
        ]
    )
    run_command(
        [
            "docker",
            "exec",
            container,
            "redis-cli",
            "ZADD",
            "autooncall:hotkeys:local-dev",
            "100",
            "order:detail:10001",
            "80",
            "user:session:10001",
        ]
    )


def seed_mysql(container: str, sql_path: Path) -> None:
    wait_for_mysql(container)
    sql = sql_path.read_text(encoding="utf-8")
    run_command(
        [
            "docker",
            "exec",
            "-i",
            container,
            "mysql",
            f"-u{MYSQL_USER}",
            f"-p{MYSQL_PASSWORD}",
            MYSQL_DATABASE,
        ],
        input_text=sql,
    )
    run_best_effort(
        [
            "docker",
            "exec",
            container,
            "mysql",
            f"-u{MYSQL_USER}",
            f"-p{MYSQL_PASSWORD}",
            MYSQL_DATABASE,
            "-e",
            (
                "UPDATE aiops_dependency_snapshots "
                "SET source='live-mysql-seed' "
                "WHERE source <> 'live-mysql-seed'; "
                "UPDATE aiops_remediation_audit "
                "SET source='live-mysql-seed' "
                "WHERE source <> 'live-mysql-seed';"
            ),
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed live Docker containers with Redis/MySQL incident evidence."
    )
    parser.add_argument("--redis-container", default=DEFAULT_REDIS_CONTAINER)
    parser.add_argument("--mysql-container", default=DEFAULT_MYSQL_CONTAINER)
    parser.add_argument("--sql", default=str(DEFAULT_SQL_PATH))
    parser.add_argument("--skip-redis", action="store_true")
    parser.add_argument("--skip-mysql", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.skip_redis:
        seed_redis(args.redis_container)
    if not args.skip_mysql:
        seed_mysql(args.mysql_container, Path(args.sql))
    print("Live incident evidence seeded into Docker Redis/MySQL containers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
