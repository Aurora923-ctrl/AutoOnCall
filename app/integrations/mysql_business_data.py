"""MySQL-backed business context store for local AIOps adapters."""

from __future__ import annotations

import json
import math
from typing import Any

from app.config import config
from app.core.resilience import run_bounded_sync_call
from app.integrations.base import ExternalAdapterError, ExternalAdapterNotFoundError
from app.integrations.mysql import MySQLStatusAdapter


class MySQLBusinessDataAdapter:
    """Read CMDB-like context, deployment history, and tickets from MySQL."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn if dsn is not None else config.resolved_mysql_dsn
        self.timeout_seconds = config.mysql_timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.dsn)

    async def query_service_catalog(self, service_name: str) -> dict[str, Any]:
        return await run_bounded_sync_call(
            "mysql-business-data",
            "query_service_catalog",
            lambda: self._query_service_catalog_sync(service_name),
            timeout_seconds=self.timeout_seconds,
        )

    async def query_deploy_history(self, service_name: str) -> dict[str, Any]:
        return await run_bounded_sync_call(
            "mysql-business-data",
            "query_deploy_history",
            lambda: self._query_deploy_history_sync(service_name),
            timeout_seconds=self.timeout_seconds,
        )

    async def search_tickets(
        self, service_name: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        return await run_bounded_sync_call(
            "mysql-business-data",
            "search_tickets",
            lambda: self._search_tickets_sync(service_name, query, limit),
            timeout_seconds=self.timeout_seconds,
        )

    def _query_service_catalog_sync(self, service_name: str) -> dict[str, Any]:
        row = self._fetch_one(
            "SELECT payload FROM aiops_service_catalog WHERE service_name=%s",
            (service_name,),
        )
        if not row:
            raise ExternalAdapterNotFoundError(f"service catalog not found for {service_name}")
        return self._json_payload(row, "payload")

    def _query_deploy_history_sync(self, service_name: str) -> dict[str, Any]:
        row = self._fetch_one(
            "SELECT payload FROM aiops_deploy_history WHERE service_name=%s",
            (service_name,),
        )
        if not row:
            raise ExternalAdapterNotFoundError(f"deployment history not found for {service_name}")
        return self._json_payload(row, "payload")

    def _search_tickets_sync(
        self,
        service_name: str,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            """
            SELECT payload
            FROM aiops_history_tickets
            WHERE service_name=%s
            ORDER BY updated_at DESC, ticket_id DESC
            LIMIT 50
            """,
            (service_name,),
        )
        tickets = [self._json_payload(row, "payload") for row in rows]
        keywords = _query_keywords(query)
        if keywords:
            filtered = [
                ticket
                for ticket in tickets
                if any(keyword in _ticket_search_text(ticket) for keyword in keywords)
            ]
            if filtered:
                tickets = filtered
        return tickets[: min(max(int(limit), 1), 20)]

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        rows = self._fetch_all(sql, params)
        return rows[0] if rows else None

    def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            import pymysql
        except ImportError as exc:
            raise ExternalAdapterError("PyMySQL is required for MySQL business data") from exc

        if not self.dsn:
            raise ExternalAdapterError("MYSQL_DSN is not configured")

        connection = pymysql.connect(
            **MySQLStatusAdapter._connection_kwargs(self.dsn),
            connect_timeout=max(1, math.ceil(self.timeout_seconds)),
            read_timeout=max(1, math.ceil(self.timeout_seconds)),
            write_timeout=max(1, math.ceil(self.timeout_seconds)),
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            with connection.cursor() as cursor:
                MySQLStatusAdapter._execute_read_only_with_params(cursor, sql, params)
                return list(cursor.fetchall())
        finally:
            connection.close()

    @staticmethod
    def _json_payload(row: dict[str, Any], field_name: str) -> dict[str, Any]:
        value = row.get(field_name)
        if isinstance(value, dict):
            return value
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        if not isinstance(value, str):
            raise ExternalAdapterError(f"MySQL business data field {field_name} is not JSON")
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ExternalAdapterError(
                f"MySQL business data field {field_name} is invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ExternalAdapterError(f"MySQL business data field {field_name} must be an object")
        return payload


def _query_keywords(query: str) -> list[str]:
    stop_words = {"or", "and", "the", "with", "service"}
    return [
        item
        for item in str(query or "").lower().replace("|", " ").replace(",", " ").split()
        if len(item) >= 3 and item not in stop_words
    ]


def _ticket_search_text(ticket: dict[str, Any]) -> str:
    return " ".join(
        [
            str(ticket.get("title") or ""),
            str(ticket.get("root_cause") or ""),
            str(ticket.get("resolution") or ""),
            str(ticket.get("customer_impact") or ""),
            str(ticket.get("business_impact") or ""),
            " ".join(str(item) for item in ticket.get("evidence", []) if item),
            " ".join(str(item) for item in ticket.get("labels", []) if item),
        ]
    ).lower()
