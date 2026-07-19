"""Generic HTTP log-search gateway adapter."""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import config
from app.integrations.base import (
    ExternalAdapterResponseError,
    adapter_success,
    bearer_headers,
    parse_duration_seconds,
    require_config,
    require_success_payload,
)


class HTTPLogGatewayAdapter:
    """Query logs through an internal HTTP gateway such as CLS/Loki/OpenSearch facade."""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url = url if url is not None else config.log_gateway_url
        self.token = token if token is not None else config.log_gateway_bearer_token
        self.timeout_seconds = timeout_seconds or config.log_gateway_timeout_seconds
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def search_logs(
        self,
        service_name: str,
        query: str,
        time_range: str,
        limit: int,
    ) -> dict[str, Any]:
        url = require_config(self.url, "LOG_GATEWAY_URL")
        bounded_limit = min(max(int(limit), 1), 1000)
        end_time_ms = int(time.time() * 1000)
        start_time_ms = end_time_ms - parse_duration_seconds(time_range) * 1000
        keyword_filters = self._extract_keyword_filters(query)
        request = {
            "service_name": service_name,
            "query": query,
            "time_range": time_range,
            "start_time": start_time_ms,
            "end_time": end_time_ms,
            "keyword_filters": keyword_filters,
            "limit": bounded_limit,
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=bearer_headers(self.token),
            transport=self.transport,
        ) as client:
            response = await client.post(url, json=request)
            response.raise_for_status()
            payload = require_success_payload(
                response.json(),
                system_name="Log gateway",
            )

        logs = payload.get("logs", payload.get("items", []))
        if not isinstance(logs, list):
            raise ExternalAdapterResponseError("Log gateway response logs/items must be an array")
        return adapter_success(
            source="log_gateway",
            summary=f"日志网关返回 {len(logs)} 条记录",
            signals={"log_count": len(logs), "keyword_count": len(keyword_filters)},
            raw=payload,
            service_name=service_name,
            query=query,
            time_range=time_range,
            start_time=start_time_ms,
            end_time=end_time_ms,
            keyword_filters=keyword_filters,
            logs={"total": len(logs), "logs": logs},
        )

    @staticmethod
    def _extract_keyword_filters(query: str) -> list[str]:
        separators = [" OR ", " or ", "|", ","]
        parts = [query or ""]
        for separator in separators:
            parts = [piece for item in parts for piece in item.split(separator)]
        return [item.strip() for item in parts if item.strip()]
