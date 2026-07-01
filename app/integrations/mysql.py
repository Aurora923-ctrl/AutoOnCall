"""Optional MySQL read-only adapter."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.config import config
from app.integrations.base import ExternalAdapterError, adapter_success, require_config


class MySQLStatusAdapter:
    """Read MySQL status using optional PyMySQL/mysqlclient-compatible drivers."""

    def __init__(self):
        self.dsn = config.resolved_mysql_dsn
        self.instance_dsns = config.mysql_instance_map
        self.timeout_seconds = config.mysql_timeout_seconds
        self.store_raw_external_payload = config.aiops_store_raw_external_payload

    @property
    def configured(self) -> bool:
        return bool(self.dsn or self.instance_dsns)

    async def query_status(self, service_name: str, mysql_instance: str = "") -> dict[str, Any]:
        dsn = self._resolve_dsn(mysql_instance)
        return await asyncio.to_thread(self._query_status_sync, service_name, mysql_instance, dsn)

    async def ping(self, mysql_instance: str = "") -> dict[str, Any]:
        """Return a lightweight SELECT 1 connectivity check for readiness endpoints."""
        dsn = self._resolve_dsn(mysql_instance)
        return await asyncio.to_thread(self._ping_sync, dsn)

    def _query_status_sync(
        self,
        service_name: str,
        mysql_instance: str,
        dsn: str,
    ) -> dict[str, Any]:
        try:
            import pymysql
        except ImportError as exc:
            raise ExternalAdapterError("PyMySQL is required for MYSQL_DSN integration") from exc

        from urllib.parse import urlparse

        parsed = urlparse(dsn)
        connection = pymysql.connect(
            host=parsed.hostname,
            port=parsed.port or 3306,
            user=parsed.username,
            password=parsed.password,
            database=(parsed.path or "/").lstrip("/") or None,
            connect_timeout=int(self.timeout_seconds),
            read_timeout=int(self.timeout_seconds),
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            with connection.cursor() as cursor:
                self._execute_read_only(
                    cursor,
                    "SHOW GLOBAL STATUS WHERE Variable_name IN "
                    "('Threads_connected', 'Max_used_connections', 'Slow_queries', "
                    "'Innodb_row_lock_waits')",
                )
                status_rows = list(cursor.fetchall())
                self._execute_read_only(cursor, "SHOW FULL PROCESSLIST")
                process_rows = [self._redact_process_row(row) for row in cursor.fetchall()]
        finally:
            connection.close()

        status = self._normalize_status_rows(status_rows)
        missing_fields = self._missing_required_status_fields(status)
        if missing_fields:
            raise ExternalAdapterError(
                "MySQL status response missing required fields: " + ", ".join(missing_fields)
            )
        active = int(status["Threads_connected"])
        max_used = int(status["Max_used_connections"])
        slow_queries = int(status["Slow_queries"])
        lock_waits = int(status["Innodb_row_lock_waits"])
        return adapter_success(
            source="mysql",
            summary=f"MySQL 当前连接数 {active}，慢查询累计 {slow_queries}",
            signals={
                "active_connections": active,
                "max_used_connections": max_used,
                "slow_queries": slow_queries,
                "lock_waits": lock_waits,
            },
            raw={
                "status": (
                    status_rows if self.store_raw_external_payload else self._compact_status(status)
                ),
                "processlist_sample": (
                    process_rows[:10]
                    if self.store_raw_external_payload
                    else self._compact_processlist(process_rows)
                ),
            },
            service_name=service_name,
            mysql_instance=mysql_instance,
            endpoint=f"{parsed.hostname}:{parsed.port or 3306}",
            slow_queries={"count": slow_queries, "status": "checked"},
            connections={"active": active, "max_used": max_used},
            processlist_sample=process_rows[:10],
            lock_waits=lock_waits,
        )

    def _ping_sync(self, dsn: str) -> dict[str, Any]:
        try:
            import pymysql
        except ImportError as exc:
            raise ExternalAdapterError("PyMySQL is required for MYSQL_DSN integration") from exc

        from urllib.parse import urlparse

        parsed = urlparse(dsn)
        connection = pymysql.connect(
            host=parsed.hostname,
            port=parsed.port or 3306,
            user=parsed.username,
            password=parsed.password,
            database=(parsed.path or "/").lstrip("/") or None,
            connect_timeout=int(self.timeout_seconds),
            read_timeout=int(self.timeout_seconds),
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            with connection.cursor() as cursor:
                self._execute_read_only(cursor, "SELECT 1")
                cursor.fetchone()
        finally:
            connection.close()
        return {
            "status": "connected",
            "message": "SELECT 1 succeeded",
            "endpoint": f"{parsed.hostname}:{parsed.port or 3306}",
        }

    def _resolve_dsn(self, mysql_instance: str = "") -> str:
        dsn = self.instance_dsns.get(mysql_instance) if mysql_instance else ""
        return require_config(dsn or self.dsn, "MYSQL_DSN, MYSQL_URL, or MYSQL_HOST")

    @classmethod
    def _execute_read_only(cls, cursor: Any, sql: str) -> None:
        cls._assert_read_only_sql(sql)
        cursor.execute(sql)

    @staticmethod
    def _assert_read_only_sql(sql: str) -> None:
        normalized = " ".join(sql.strip().split()).lower()
        if not normalized.startswith(("show ", "select ")):
            raise ExternalAdapterError("MySQL adapter only allows read-only SHOW/SELECT statements")
        forbidden = re.search(
            r"\b(insert|update|delete|drop|alter|truncate|create|replace|grant|revoke|load)\b",
            normalized,
        )
        if forbidden:
            raise ExternalAdapterError("MySQL adapter blocked a non-read-only SQL statement")

    @staticmethod
    def _normalize_status_rows(status_rows: list[dict[str, Any]]) -> dict[str, str]:
        status: dict[str, str] = {}
        for row in status_rows:
            if not isinstance(row, dict):
                raise ExternalAdapterError("MySQL status row has an invalid format")
            variable = row.get("Variable_name")
            value = row.get("Value")
            if variable is None or value is None:
                raise ExternalAdapterError("MySQL status row missing Variable_name or Value")
            status[str(variable)] = str(value)
        return status

    @staticmethod
    def _missing_required_status_fields(status: dict[str, str]) -> list[str]:
        return [
            field_name
            for field_name in [
                "Threads_connected",
                "Max_used_connections",
                "Slow_queries",
                "Innodb_row_lock_waits",
            ]
            if field_name not in status
        ]

    @staticmethod
    def _compact_status(status: dict[str, str]) -> dict[str, str]:
        keys = {
            "Threads_connected",
            "Max_used_connections",
            "Slow_queries",
            "Innodb_row_lock_waits",
        }
        return {key: value for key, value in status.items() if key in keys}

    @staticmethod
    def _compact_processlist(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compact_rows: list[dict[str, Any]] = []
        for row in rows[:5]:
            compact_rows.append(
                {
                    "Id": row.get("Id"),
                    "User": row.get("User"),
                    "Host": row.get("Host"),
                    "db": row.get("db"),
                    "Command": row.get("Command"),
                    "Time": row.get("Time"),
                    "State": row.get("State"),
                    "Info": row.get("Info"),
                }
            )
        return compact_rows

    @classmethod
    def _redact_process_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(row)
        if redacted.get("Info"):
            redacted["Info"] = cls._redact_sql(str(redacted["Info"]))
        return redacted

    @staticmethod
    def _redact_sql(sql: str) -> str:
        redacted = re.sub(r"'[^']*'", "'?'", sql)
        redacted = re.sub(r'"[^"]*"', '"?"', redacted)
        return re.sub(r"\b\d+\b", "?", redacted)
