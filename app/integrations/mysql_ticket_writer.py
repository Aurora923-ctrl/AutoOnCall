"""Write-only MySQL adapter for approved ticket creation."""

from __future__ import annotations

import json
import math
from typing import Any

from app.config import config
from app.core.resilience import run_bounded_sync_call
from app.integrations.base import ExternalAdapterError
from app.integrations.mysql import MySQLStatusAdapter


class MySQLTicketWriterAdapter:
    """Persist tickets through a deliberately separate write-capable dependency."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn if dsn is not None else config.resolved_mysql_dsn
        self.timeout_seconds = config.mysql_timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.dsn)

    async def create_ticket(
        self,
        *,
        service_name: str,
        title: str,
        description: str,
        severity: str,
        approval_id: str = "",
        risk_action: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return await run_bounded_sync_call(
            "mysql-ticket-writer",
            "create_ticket",
            lambda: self._create_ticket_sync(
                service_name,
                title,
                description,
                severity,
                approval_id,
                risk_action,
                idempotency_key,
            ),
            timeout_seconds=self.timeout_seconds,
        )

    def _create_ticket_sync(
        self,
        service_name: str,
        title: str,
        description: str,
        severity: str,
        approval_id: str,
        risk_action: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        try:
            import pymysql
        except ImportError as exc:
            raise ExternalAdapterError("PyMySQL is required for ticket creation") from exc
        if not self.dsn:
            raise ExternalAdapterError("MYSQL_DSN is not configured")

        ticket_id = idempotency_key or f"{service_name}:{title}"
        payload = {
            "ticket_id": ticket_id,
            "service_name": service_name,
            "title": title,
            "description": description,
            "severity": severity,
            "approval_id": approval_id,
            "risk_action": risk_action,
            "status": "created",
        }
        connection = pymysql.connect(
            **MySQLStatusAdapter._connection_kwargs(self.dsn),
            connect_timeout=max(1, math.ceil(self.timeout_seconds)),
            read_timeout=max(1, math.ceil(self.timeout_seconds)),
            write_timeout=max(1, math.ceil(self.timeout_seconds)),
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO aiops_history_tickets
                        (ticket_id, service_name, title, severity, root_cause, resolution,
                         customer_impact, labels_text, payload)
                    VALUES (%s, %s, %s, %s, '', '', '', '', %s)
                    ON DUPLICATE KEY UPDATE
                        title=VALUES(title),
                        severity=VALUES(severity),
                        payload=VALUES(payload),
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        ticket_id,
                        service_name,
                        title,
                        severity,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return payload
