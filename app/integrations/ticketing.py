"""HTTP ticket-system adapter."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import config
from app.integrations.base import (
    ExternalAdapterResponseError,
    adapter_success,
    bearer_headers,
    require_config,
    require_success_payload,
)
from app.integrations.mysql_business_data import MySQLBusinessDataAdapter
from app.integrations.mysql_ticket_writer import MySQLTicketWriterAdapter


class TicketingAdapter:
    """Search similar historical incidents through MySQL or an internal ticket API."""

    def __init__(
        self,
        url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        mysql_adapter: MySQLBusinessDataAdapter | None = None,
        mysql_writer: MySQLTicketWriterAdapter | None = None,
    ):
        self.url = url if url is not None else config.ticket_api_url
        self.token = config.ticket_api_bearer_token
        self.timeout_seconds = config.ticket_api_timeout_seconds
        self.transport = transport
        self.mysql_adapter = mysql_adapter or MySQLBusinessDataAdapter()
        self.mysql_writer = mysql_writer or MySQLTicketWriterAdapter()

    @property
    def configured(self) -> bool:
        return bool(self.url or self.mysql_adapter.configured)

    async def search_history(
        self, service_name: str, query: str = "", limit: int = 5
    ) -> dict[str, Any]:
        if self.url:
            url = require_config(self.url, "TICKET_API_URL")
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers=bearer_headers(self.token),
                transport=self.transport,
            ) as client:
                response = await client.get(
                    url,
                    params={"service_name": service_name, "query": query, "limit": limit},
                )
                response.raise_for_status()
                payload = require_success_payload(
                    response.json(),
                    system_name="Ticket history API",
                )
            tickets = payload.get("tickets", payload.get("items", []))
            if not isinstance(tickets, list):
                raise ExternalAdapterResponseError(
                    "Ticket history response tickets/items must be an array"
                )
            normalized_tickets = [
                self._ticket_summary(ticket)
                for ticket in self._filter_tickets(tickets, service_name, query, limit)
            ]
        else:
            normalized_tickets = [
                self._ticket_summary(ticket)
                for ticket in await self.mysql_adapter.search_tickets(service_name, query, limit)
            ]
            payload = {"items": normalized_tickets}
        return adapter_success(
            source="ticket_api",
            summary=f"工单系统返回 {len(normalized_tickets)} 条 {service_name} 相似故障",
            signals={"ticket_count": len(normalized_tickets)},
            raw=payload,
            service_name=service_name,
            tickets=normalized_tickets,
        )

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
        if not self.url:
            ticket = self._ticket_summary(
                await self.mysql_writer.create_ticket(
                    service_name=service_name,
                    title=title,
                    description=description,
                    severity=severity,
                    approval_id=approval_id,
                    risk_action=risk_action,
                    idempotency_key=idempotency_key,
                )
            )
            return adapter_success(
                source="ticket_api",
                summary=f"工单创建成功: {ticket.get('ticket_id', '')}",
                signals={"duplicate": False, "ticket_count": 1},
                raw={"ticket": ticket},
                service_name=service_name,
                ticket=ticket,
                ticket_status="created",
                duplicate=False,
            )

        url = require_config(self.url, "TICKET_API_URL")
        request = {
            "service_name": service_name,
            "title": title,
            "description": description,
            "severity": severity,
            "approval_id": approval_id,
            "risk_action": risk_action,
            "idempotency_key": idempotency_key,
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=bearer_headers(self.token),
            transport=self.transport,
        ) as client:
            response = await client.post(url, json=request)
            if response.status_code == 409:
                payload = require_success_payload(
                    response.json(),
                    system_name="Ticket creation API",
                )
                ticket = self._ticket_summary(payload.get("ticket", payload))
                return adapter_success(
                    source="ticket_api",
                    summary="工单系统发现重复工单，未创建新记录",
                    signals={"duplicate": True, "ticket_count": 1},
                    raw=payload,
                    service_name=service_name,
                    ticket=ticket,
                    ticket_status="duplicate",
                    duplicate=True,
                )
            response.raise_for_status()
            payload = require_success_payload(
                response.json(),
                system_name="Ticket creation API",
            )
        ticket = self._ticket_summary(payload.get("ticket", payload))
        if not ticket["ticket_id"]:
            raise ExternalAdapterResponseError("Ticket creation response missing ticket_id")
        return adapter_success(
            source="ticket_api",
            summary=f"工单创建成功: {ticket.get('ticket_id', '')}",
            signals={"duplicate": False, "ticket_count": 1},
            raw=payload,
            service_name=service_name,
            ticket=ticket,
            ticket_status="created",
            duplicate=False,
        )

    @staticmethod
    def _ticket_summary(ticket: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticket_id": ticket.get("ticket_id") or ticket.get("id") or "",
            "service_name": ticket.get("service_name", ""),
            "title": ticket.get("title", ""),
            "status": ticket.get("status", ""),
            "severity": ticket.get("severity", ""),
            "approval_id": ticket.get("approval_id", ""),
            "risk_action": ticket.get("risk_action", ""),
            "root_cause": ticket.get("root_cause", ""),
            "resolution": ticket.get("resolution", ""),
            "customer_impact": ticket.get("customer_impact", ""),
            "business_impact": ticket.get("business_impact", ""),
            "impacted_endpoints": ticket.get("impacted_endpoints", []),
            "evidence": ticket.get("evidence", []),
            "timeline": ticket.get("timeline", []),
            "prevention": ticket.get("prevention", []),
            "labels": ticket.get("labels", []),
        }

    @staticmethod
    def _filter_tickets(
        tickets: Any,
        service_name: str,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not isinstance(tickets, list):
            return []
        service_filtered = [
            ticket
            for ticket in tickets
            if isinstance(ticket, dict)
            and (not ticket.get("service_name") or str(ticket.get("service_name")) == service_name)
        ]
        keywords = _query_keywords(query)
        if keywords:
            query_filtered = [
                ticket
                for ticket in service_filtered
                if any(keyword in _ticket_search_text(ticket) for keyword in keywords)
            ]
            if query_filtered:
                service_filtered = query_filtered
        return service_filtered[: min(max(int(limit), 1), 20)]


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
