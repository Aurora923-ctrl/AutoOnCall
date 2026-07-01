"""Prometheus HTTP API adapter."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import config
from app.integrations.base import (
    ExternalAdapterError,
    adapter_success,
    bearer_headers,
    first_float,
    require_config,
)


class PrometheusMetricsAdapter:
    """Read service metrics from Prometheus using configurable PromQL templates."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = (base_url if base_url is not None else config.prometheus_base_url).rstrip(
            "/"
        )
        self.token = token if token is not None else config.prometheus_bearer_token
        self.timeout_seconds = timeout_seconds or config.prometheus_timeout_seconds
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def query_service_metrics(
        self,
        service_name: str,
        time_range: str = "10m",
        interval: str = "1m",
    ) -> dict[str, Any]:
        base_url = require_config(self.base_url, "PROMETHEUS_BASE_URL")
        queries = {
            "qps": config.prometheus_qps_query,
            "error_rate": config.prometheus_error_rate_query,
            "p95_latency_ms": config.prometheus_p95_query,
            "cpu_usage_percent": config.prometheus_cpu_query,
            "memory_working_set_bytes": config.prometheus_memory_query,
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=bearer_headers(self.token),
            transport=self.transport,
        ) as client:
            values: dict[str, float] = {}
            empty_queries: list[str] = []
            for name, template in queries.items():
                value, has_data = await self._query_instant(
                    client, base_url, template, service_name
                )
                values[name] = value
                if not has_data:
                    empty_queries.append(name)

        qps = {"current": round(values["qps"], 4)}
        p95_latency_ms = {
            "current": round(values["p95_latency_ms"], 2),
            "threshold": 1000,
            "status": "high" if values["p95_latency_ms"] >= 1000 else "normal",
        }
        error_rate = {
            "current": round(values["error_rate"], 6),
            "threshold": 0.01,
            "status": "high" if values["error_rate"] >= 0.01 else "normal",
        }
        cpu_current = round(values["cpu_usage_percent"], 2)
        memory_current = round(values["memory_working_set_bytes"], 2)
        cpu = {
            "metric_name": "cpu_usage_percent",
            "statistics": {"current": cpu_current},
        }
        memory = {
            "metric_name": "memory_working_set_bytes",
            "statistics": {"current": memory_current},
        }
        return adapter_success(
            source="prometheus",
            summary=(
                f"{service_name} prometheus P95={values['p95_latency_ms']:.2f}ms, "
                f"5xx={values['error_rate'] * 100:.2f}%, empty={len(empty_queries)}"
            ),
            signals={
                "qps": qps["current"],
                "p95_latency_ms": p95_latency_ms["current"],
                "error_rate": error_rate["current"],
                "cpu_usage_percent": cpu_current,
                "memory_working_set_bytes": memory_current,
            },
            raw={"promql_values": values, "empty_queries": empty_queries},
            service_name=service_name,
            time_range=time_range,
            interval=interval,
            empty_queries=empty_queries,
            qps=qps,
            p95_latency_ms=p95_latency_ms,
            error_rate=error_rate,
            cpu=cpu,
            memory=memory,
        )

    async def _query_instant(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        query_template: str,
        service_name: str,
    ) -> tuple[float, bool]:
        query = query_template.replace("{service_name}", service_name)
        response = await client.get(f"{base_url}/api/v1/query", params={"query": query})
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise ExternalAdapterError(f"Prometheus query failed: {payload}")
        result = payload.get("data", {}).get("result", [])
        if not isinstance(result, list) or not result:
            return 0.0, False
        first_result = result[0]
        if not isinstance(first_result, dict):
            return 0.0, False
        value_payload = first_result.get("value", [None, 0])
        if not isinstance(value_payload, (list, tuple)) or len(value_payload) < 2:
            return 0.0, False
        value = value_payload[1]
        return first_float(value), True
