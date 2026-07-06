"""Loki HTTP API adapter for local and production log evidence."""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from app.config import config
from app.integrations.base import (
    adapter_success,
    bearer_headers,
    parse_duration_seconds,
    require_config,
)


class LokiLogAdapter:
    """Query Grafana Loki through `/loki/api/v1/query_range`."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url if base_url is not None else config.loki_base_url
        self.token = token if token is not None else config.loki_bearer_token
        self.timeout_seconds = timeout_seconds or config.loki_timeout_seconds
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def search_logs(
        self,
        service_name: str,
        query: str,
        time_range: str,
        limit: int,
    ) -> dict[str, Any]:
        base_url = require_config(self.base_url, "LOKI_BASE_URL")
        bounded_limit = min(max(int(limit), 1), 1000)
        end_ns = int(time.time() * 1_000_000_000)
        start_ns = end_ns - parse_duration_seconds(time_range) * 1_000_000_000
        logql = self._build_logql(service_name, query)
        url = f"{base_url}/loki/api/v1/query_range"

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=bearer_headers(self.token),
            transport=self.transport,
        ) as client:
            response = await client.get(
                url,
                params={
                    "query": logql,
                    "start": str(start_ns),
                    "end": str(end_ns),
                    "limit": str(bounded_limit),
                    "direction": "backward",
                },
            )
            response.raise_for_status()
            payload = response.json()

        logs = self._normalize_streams(payload)
        return adapter_success(
            source="loki",
            summary=f"Loki 返回 {len(logs)} 条 {service_name} 日志",
            signals={
                "log_count": len(logs),
                "stream_count": len(payload.get("data", {}).get("result", [])),
            },
            raw=payload,
            service_name=service_name,
            query=query,
            logql=logql,
            time_range=time_range,
            start_time_ns=start_ns,
            end_time_ns=end_ns,
            logs={"total": len(logs), "logs": logs},
        )

    @staticmethod
    def _build_logql(service_name: str, query: str) -> str:
        selector = f'{{service="{_escape_logql_string(service_name)}"}}'
        keywords = _extract_log_keywords(query)
        if not keywords:
            return selector
        regex = "|".join(re.escape(keyword) for keyword in keywords)
        return f'{selector} |~ "{_escape_logql_string(regex)}"'

    @staticmethod
    def _normalize_streams(payload: dict[str, Any]) -> list[dict[str, Any]]:
        results = payload.get("data", {}).get("result", [])
        logs: list[dict[str, Any]] = []
        if not isinstance(results, list):
            return logs
        for stream in results:
            labels = stream.get("stream", {}) if isinstance(stream, dict) else {}
            values = stream.get("values", []) if isinstance(stream, dict) else []
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, (list, tuple)) or len(value) < 2:
                    continue
                timestamp_ns, line = value[0], value[1]
                logs.append(
                    {
                        "timestamp_ns": str(timestamp_ns),
                        "message": str(line),
                        "labels": labels if isinstance(labels, dict) else {},
                    }
                )
        return logs


def _escape_logql_string(value: str) -> str:
    """Escape a value for a quoted LogQL string literal."""
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _extract_log_keywords(query: str) -> list[str]:
    """Split a user query into ordered, de-duplicated log keywords."""
    seen: set[str] = set()
    keywords = []
    for item in re.split(r"\s+OR\s+|\||,", query or "", flags=re.IGNORECASE):
        keyword = item.strip()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        keywords.append(keyword)
    return keywords
