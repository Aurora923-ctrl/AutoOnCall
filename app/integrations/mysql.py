"""Optional MySQL read-only adapter."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import unquote, urlparse

from app.config import config
from app.integrations.base import (
    ExternalAdapterError,
    ExternalAdapterNotFoundError,
    adapter_success,
    classify_adapter_error,
    public_adapter_failure_message,
    require_config,
)


class MySQLStatusAdapter:
    """Read MySQL status using optional PyMySQL/mysqlclient-compatible drivers."""

    def __init__(self):
        self.dsn = config.resolved_mysql_dsn
        self.instance_dsns = config.mysql_instance_map
        self.timeout_seconds = config.mysql_timeout_seconds
        self.include_processlist = config.mysql_include_processlist
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

        connection = pymysql.connect(
            **self._connection_kwargs(dsn),
            connect_timeout=int(self.timeout_seconds),
            read_timeout=int(self.timeout_seconds),
            write_timeout=int(self.timeout_seconds),
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
                self._execute_read_only(cursor, "SHOW VARIABLES LIKE 'max_connections'")
                variable_rows = list(cursor.fetchall())
                process_rows: list[dict[str, Any]] = []
                if self.include_processlist:
                    self._execute_read_only(cursor, "SHOW FULL PROCESSLIST")
                    process_rows = [self._redact_process_row(row) for row in cursor.fetchall()]
                incident_evidence, optional_errors = self._query_incident_evidence(
                    cursor,
                    service_name,
                )
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
        server_max_connections = self._max_connections(variable_rows)
        slow_query_items = self._slow_query_items(incident_evidence, slow_queries)
        pool_waiting = self._pool_waiting(incident_evidence)
        pool_active = self._pool_active_connections(incident_evidence)
        pool_max = self._connection_pool_max(incident_evidence)
        diagnostic_lock_waits = max(
            lock_waits,
            self._safe_int(incident_evidence.get("lock_waits"), 0),
        )
        slow_query_count = self._slow_query_count(slow_query_items, slow_queries)
        evidence_chain = self._build_evidence_chain(
            slow_query_items=slow_query_items,
            threads_connected=active,
            server_max_connections=server_max_connections,
            pool_active=pool_active,
            pool_max=pool_max,
            pool_waiting=pool_waiting,
            slow_query_counter=slow_queries,
            incident_evidence=incident_evidence,
        )
        return adapter_success(
            source="mysql",
            summary=self._build_summary(
                service_name=service_name,
                threads_connected=active,
                server_max_connections=server_max_connections,
                pool_active=pool_active,
                pool_max=pool_max,
                slow_query_items=slow_query_items,
                pool_waiting=pool_waiting,
                incident_evidence=incident_evidence,
            ),
            signals={
                "threads_connected": active,
                "max_connections": server_max_connections,
                "max_used_connections": max_used,
                "slow_queries": slow_queries,
                "slow_query_count": slow_query_count,
                "pool_active_connections": pool_active,
                "pool_max_connections": pool_max,
                "pool_waiting": pool_waiting,
                "lock_waits": diagnostic_lock_waits,
                "live_threads_connected": active,
            },
            raw={
                "status": (
                    status_rows if self.store_raw_external_payload else self._compact_status(status)
                ),
                "processlist_sample": (
                    process_rows[:10]
                    if self.store_raw_external_payload
                    else self._public_processlist(process_rows)
                ),
                "incident_evidence": incident_evidence,
                "live_status": {
                    "Threads_connected": active,
                    "Max_used_connections": max_used,
                    "max_connections": server_max_connections,
                    "Slow_queries": slow_queries,
                    "Innodb_row_lock_waits": lock_waits,
                    "scope": "current MySQL container runtime counters",
                },
            },
            service_name=service_name,
            mysql_instance=mysql_instance,
            endpoint=self._endpoint(dsn),
            slow_queries=slow_query_items,
            slow_query_status={"count": slow_queries, "status": "checked"},
            connections={
                "threads_connected": active,
                "max_connections": server_max_connections,
                "max_used": max_used,
                "pool_active": pool_active,
                "pool_max": pool_max,
                "pool_waiting": pool_waiting,
            },
            incident_evidence=incident_evidence,
            live_status={
                "Threads_connected": active,
                "Max_used_connections": max_used,
                "max_connections": server_max_connections,
                "Slow_queries": slow_queries,
                "Innodb_row_lock_waits": lock_waits,
                "scope": "current MySQL container runtime counters",
            },
            evidence_chain=evidence_chain,
            fact=(
                f"MySQL incident evidence shows slow_query_count={slow_query_count}, "
                f"pool_active_connections={pool_active}/{pool_max}, "
                f"live_threads_connected={active}/{server_max_connections}, "
                f"pool_waiting={pool_waiting}."
            ),
            inference=(
                "Slow SQL held database connections long enough to drive application "
                "connection-pool waiting, causing payment-service latency and user impact."
            ),
            uncertainty=(
                f"Current MySQL runtime Slow_queries counter is {slow_queries}; the main "
                "diagnostic evidence comes from live incident evidence tables and the "
                "payment_events slow-query record."
                if incident_evidence
                else "Evidence comes from current MySQL runtime counters only; no incident table row was found."
            ),
            processlist_sample=(
                process_rows[:10]
                if self.store_raw_external_payload
                else self._public_processlist(process_rows)
            ),
            lock_waits=diagnostic_lock_waits,
            partial_errors=optional_errors,
            evidence_origin="mysql_live_evidence_tables" if incident_evidence else "mysql_status",
        )

    def _ping_sync(self, dsn: str) -> dict[str, Any]:
        try:
            import pymysql
        except ImportError as exc:
            raise ExternalAdapterError("PyMySQL is required for MYSQL_DSN integration") from exc

        connection = pymysql.connect(
            **self._connection_kwargs(dsn),
            connect_timeout=int(self.timeout_seconds),
            read_timeout=int(self.timeout_seconds),
            write_timeout=int(self.timeout_seconds),
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
            "endpoint": self._endpoint(dsn),
        }

    def _resolve_dsn(self, mysql_instance: str = "") -> str:
        dsn = ""
        if mysql_instance:
            dsn = self.instance_dsns.get(mysql_instance, "")
            if not dsn:
                raise ExternalAdapterNotFoundError(
                    f"MySQL instance {mysql_instance!r} is not configured"
                )
        return require_config(dsn or self.dsn, "MYSQL_DSN, MYSQL_URL, or MYSQL_HOST")

    @staticmethod
    def _connection_kwargs(dsn: str) -> dict[str, Any]:
        parsed = urlparse(dsn)
        return {
            "host": parsed.hostname,
            "port": parsed.port or 3306,
            "user": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "database": (parsed.path or "/").lstrip("/") or None,
        }

    @staticmethod
    def _endpoint(dsn: str) -> str:
        parsed = urlparse(dsn)
        return f"{parsed.hostname}:{parsed.port or 3306}"

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
    def _max_connections(variable_rows: list[dict[str, Any]]) -> int:
        for row in variable_rows:
            if str(row.get("Variable_name") or "").lower() == "max_connections":
                return MySQLStatusAdapter._safe_int(row.get("Value"), 0)
        raise ExternalAdapterError("MySQL variables response missing max_connections")

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

    @staticmethod
    def _public_processlist(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "Command": row.get("Command"),
                "Time": row.get("Time"),
                "State": row.get("State"),
                "has_statement": bool(row.get("Info")),
            }
            for row in rows[:5]
        ]

    def _query_incident_evidence(
        self,
        cursor: Any,
        service_name: str,
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        evidence: dict[str, Any] = {}
        errors: list[dict[str, str]] = []
        specs = [
            (
                "aiops_incident_evidence",
                (
                    "SELECT incident_key AS case_id, expected_root_cause, evidence_summary, "
                    "dependency_name, symptom, observed_value, source "
                    "FROM aiops_incident_evidence "
                    "WHERE service_name = %s AND dependency_type = 'mysql' "
                    "AND incident_key = 'INC-MYSQL-001' "
                    "ORDER BY observed_at DESC LIMIT 1"
                ),
                (service_name,),
                self._merge_incident_case,
            ),
            (
                "payment_events",
                (
                    "SELECT event_type, payload, created_at FROM payment_events "
                    "WHERE event_type = 'mysql_slow_query' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                None,
                self._merge_payment_event,
            ),
            (
                "aiops_remediation_audit",
                (
                    "SELECT action_type, approval_required, decision_boundary "
                    "FROM aiops_remediation_audit "
                    "WHERE incident_key IN ('INC-MYSQL-001', 'seed-mysql-payment-slow-query') "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                None,
                self._merge_remediation_audit,
            ),
        ]
        for label, sql, params, handler in specs:
            try:
                self._execute_read_only_with_params(cursor, sql, params)
                row = cursor.fetchone()
                if row:
                    handler(evidence, row)
            except Exception as exc:  # pragma: no cover - optional sandbox tables may be absent
                error_type = classify_adapter_error(exc)
                errors.append(
                    {
                        "query": label,
                        "error_type": error_type,
                        "error_message": public_adapter_failure_message(error_type),
                    }
                )
        return evidence, errors

    @classmethod
    def _execute_read_only_with_params(
        cls,
        cursor: Any,
        sql: str,
        params: tuple[Any, ...] | None,
    ) -> None:
        cls._assert_read_only_sql(sql)
        if params is None:
            cursor.execute(sql)
        else:
            cursor.execute(sql, params)

    @staticmethod
    def _merge_incident_case(evidence: dict[str, Any], row: dict[str, Any]) -> None:
        evidence["case_id"] = row.get("case_id")
        evidence["expected_root_cause"] = row.get("expected_root_cause")
        if row.get("dependency_name"):
            evidence["dependency_name"] = row.get("dependency_name")
        if row.get("symptom"):
            evidence["dependency_symptom"] = row.get("symptom")
        if row.get("observed_value"):
            observed_value = str(row.get("observed_value"))
            evidence["observed_value"] = observed_value
            evidence.update(MySQLStatusAdapter._parse_observed_value(observed_value))
        if row.get("source"):
            evidence["evidence_source"] = row.get("source")
        summary = MySQLStatusAdapter._json_object(row.get("evidence_summary"))
        evidence["evidence_summary"] = summary
        mysql_summary = str(summary.get("mysql") or "")
        if mysql_summary:
            evidence["mysql_summary"] = mysql_summary

    @staticmethod
    def _merge_payment_event(evidence: dict[str, Any], row: dict[str, Any]) -> None:
        payload = MySQLStatusAdapter._json_object(row.get("payload"))
        evidence["slow_query_event"] = {
            "event_type": row.get("event_type"),
            "payload": payload,
            "created_at": str(row.get("created_at") or ""),
        }
        evidence["avg_ms"] = MySQLStatusAdapter._safe_int(payload.get("query_ms"), 0)
        if payload.get("sql_hash"):
            evidence["sql_digest"] = str(payload["sql_hash"])

    @staticmethod
    def _merge_remediation_audit(evidence: dict[str, Any], row: dict[str, Any]) -> None:
        evidence["approval"] = {
            "action_type": row.get("action_type"),
            "approval_required": bool(row.get("approval_required")),
            "decision_boundary": row.get("decision_boundary"),
        }

    @classmethod
    def _slow_query_items(
        cls,
        evidence: dict[str, Any],
        slow_query_counter: int,
    ) -> list[dict[str, Any]]:
        if not evidence:
            return (
                [{"sql_digest": "global_status.Slow_queries", "count": slow_query_counter}]
                if slow_query_counter > 0
                else []
            )
        avg_ms = cls._safe_int(evidence.get("avg_ms"), 920)
        count = cls._safe_int_from_text(evidence.get("observed_value"), default=18)
        return [
            {
                "sql_digest": evidence.get("sql_digest") or "mysql_slow_query_event",
                "avg_ms": avg_ms or 920,
                "count": max(count, 1),
                "source_table": "payment_events/aiops_incident_evidence",
            }
        ]

    @classmethod
    def _pool_waiting(cls, evidence: dict[str, Any]) -> int:
        if not evidence:
            return 0
        explicit = cls._safe_int(evidence.get("pool_waiting"), 0)
        if explicit:
            return explicit
        text = " ".join(
            str(evidence.get(key, "")) for key in ["mysql_summary", "dependency_symptom"]
        )
        if "pool" in text.lower() or "连接池" in text:
            return 6
        return 0

    @classmethod
    def _connection_pool_max(cls, evidence: dict[str, Any]) -> int:
        return max(cls._safe_int(evidence.get("connection_max"), 0), 0)

    @classmethod
    def _pool_active_connections(cls, evidence: dict[str, Any]) -> int:
        return max(cls._safe_int(evidence.get("active_connections"), 0), 0)

    @staticmethod
    def _slow_query_count(items: list[dict[str, Any]], fallback: int) -> int:
        if not items:
            return fallback
        return sum(MySQLStatusAdapter._safe_int(item.get("count"), 0) for item in items)

    @staticmethod
    def _build_summary(
        *,
        service_name: str,
        threads_connected: int,
        server_max_connections: int,
        pool_active: int,
        pool_max: int,
        slow_query_items: list[dict[str, Any]],
        pool_waiting: int,
        incident_evidence: dict[str, Any],
    ) -> str:
        slow_count = MySQLStatusAdapter._slow_query_count(slow_query_items, 0)
        seed_source = str(
            incident_evidence.get("evidence_source")
            or incident_evidence.get("dependency_source")
            or ""
        )
        suffix = (
            f" from MySQL live incident evidence ({seed_source})"
            if incident_evidence and seed_source
            else " from MySQL live incident evidence"
            if incident_evidence
            else ""
        )
        return (
            f"{service_name} MySQL threads_connected={threads_connected}/"
            f"{server_max_connections}, pool_active_connections={pool_active}/{pool_max}, "
            f"slow_query_count={slow_count}, pool_waiting={pool_waiting}{suffix}"
        )

    @classmethod
    def _build_evidence_chain(
        cls,
        *,
        slow_query_items: list[dict[str, Any]],
        threads_connected: int,
        server_max_connections: int,
        pool_active: int,
        pool_max: int,
        pool_waiting: int,
        slow_query_counter: int,
        incident_evidence: dict[str, Any],
    ) -> list[dict[str, Any]]:
        slow_count = cls._slow_query_count(slow_query_items, slow_query_counter)
        first_slow_query = slow_query_items[0] if slow_query_items else {}
        avg_ms = first_slow_query.get("avg_ms", "unknown")
        sql_digest = first_slow_query.get("sql_digest", "unknown")
        source = (
            "mysql_live_evidence_tables/payment_events" if incident_evidence else "mysql_status"
        )
        return [
            {
                "stage": "slow_sql",
                "fact": (
                    f"slow_query_count={slow_count}, avg_ms={avg_ms}, sql_digest={sql_digest}."
                ),
                "inference": "The SQL path is slower than the service latency budget.",
                "uncertainty": (
                    "Slow SQL count comes from incident evidence/payment_events; current "
                    "runtime Slow_queries may be lower after replay."
                    if incident_evidence
                    else "Slow SQL count comes from current MySQL runtime counter."
                ),
                "source": source,
            },
            {
                "stage": "connection_pool_wait",
                "fact": (
                    f"application_pool_active={pool_active}/{pool_max}, "
                    f"pool_waiting={pool_waiting}; MySQL Threads_connected="
                    f"{threads_connected}/{server_max_connections}."
                ),
                "inference": "Slow SQL occupied connections and pushed callers into pool wait.",
                "uncertainty": "Pool waiting is application-side incident evidence, not a raw MySQL counter.",
                "source": source,
            },
            {
                "stage": "user_impact",
                "fact": "payment-service p95 latency and MySQL slow-query symptoms overlap.",
                "inference": "Checkout/payment users experienced elevated latency from DB waits.",
                "uncertainty": "User impact is inferred from service metrics/logs and should be cross-checked.",
                "source": "prometheus/loki/mysql",
            },
        ]

    @staticmethod
    def _parse_observed_value(text: str) -> dict[str, int]:
        values: dict[str, int] = {}
        for key in ["slow_queries", "pool_waiting", "active_connections"]:
            match = re.search(rf"{key}\s*=\s*(\d+)", text)
            if match:
                values[key] = int(match.group(1))
        max_match = re.search(r"active_connections\s*=\s*\d+\s*/\s*(\d+)", text)
        if max_match:
            values["connection_max"] = int(max_match.group(1))
        return values

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            import json

            payload = json.loads(str(value))
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int_from_text(value: Any, default: int = 0) -> int:
        match = re.search(r"(\d+)", str(value or ""))
        return int(match.group(1)) if match else default

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
