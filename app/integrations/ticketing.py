"""HTTP ticket-system adapter."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import config
from app.integrations.base import adapter_success, bearer_headers, require_config


class TicketingAdapter:
    """Search similar historical incidents through an internal ticket API."""

    def __init__(self, url: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url if url is not None else config.ticket_api_url
        self.token = config.ticket_api_bearer_token
        self.timeout_seconds = config.ticket_api_timeout_seconds
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def search_history(
        self, service_name: str, query: str = "", limit: int = 5
    ) -> dict[str, Any]:
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
            payload = response.json()
        tickets = payload.get("tickets", payload.get("items", []))
        normalized_tickets = [self._ticket_summary(ticket) for ticket in tickets]
        return adapter_success(
            source="ticket_api",
            summary=f"工单系统返回 {len(normalized_tickets)} 条相似故障",
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
                payload = response.json()
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
            payload = response.json()
        ticket = self._ticket_summary(payload.get("ticket", payload))
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
            "title": ticket.get("title", ""),
            "status": ticket.get("status", ""),
            "severity": ticket.get("severity", ""),
            "approval_id": ticket.get("approval_id", ""),
            "risk_action": ticket.get("risk_action", ""),
            "root_cause": ticket.get("root_cause", ""),
            "resolution": ticket.get("resolution", ""),
        }
