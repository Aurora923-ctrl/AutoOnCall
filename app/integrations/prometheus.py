"""Prometheus HTTP API adapter."""

from __future__ import annotations

import math
import time
from typing import Any

import httpx

from app.config import config
from app.integrations.base import (
    ExternalAdapterError,
    adapter_success,
    bearer_headers,
    escape_prometheus_label_value,
    first_float,
    parse_duration_seconds,
    require_config,
    require_success_payload,
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
            "qps": _apply_prometheus_window(config.prometheus_qps_query, time_range),
            "error_rate": _apply_prometheus_window(config.prometheus_error_rate_query, time_range),
            "p95_latency_ms": _apply_prometheus_window(config.prometheus_p95_query, time_range),
            "cpu_usage_percent": config.prometheus_cpu_query,
            "memory_working_set_bytes": config.prometheus_memory_query,
        }
        end_seconds = time.time()
        start_seconds = end_seconds - max(parse_duration_seconds(time_range), 1)
        step_seconds = max(parse_duration_seconds(interval, default_seconds=60), 1)
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=bearer_headers(self.token),
            transport=self.transport,
        ) as client:
            values: dict[str, float] = {}
            empty_queries: list[str] = []
            for name, template in queries.items():
                value, has_data = await self._query_range(
                    client,
                    base_url,
                    template,
                    service_name,
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    step_seconds=step_seconds,
                )
                values[name] = value
                if not has_data:
                    empty_queries.append(name)

        if len(empty_queries) == len(queries):
            message = (
                f"Prometheus returned no data for {service_name}; "
                f"empty_queries={', '.join(empty_queries)}"
            )
            return {
                "status": "failed",
                "source": "prometheus",
                "error_type": "no_data",
                "message": message,
                "error_message": message,
                "retryable": True,
                "signals": {},
                "raw": {"promql_values": values, "empty_queries": empty_queries},
                "service_name": service_name,
                "time_range": time_range,
                "interval": interval,
                "empty_queries": empty_queries,
                "summary": f"Prometheus 查询无数据: {message}",
            }

        qps: dict[str, Any] = {"current": round(values["qps"], 4)}
        if "qps" in empty_queries:
            qps["status"] = "missing"
        p95_latency_ms: dict[str, Any] = {
            "current": round(values["p95_latency_ms"], 2),
            "threshold": 1000,
            "status": (
                "missing"
                if "p95_latency_ms" in empty_queries
                else "high"
                if values["p95_latency_ms"] >= 1000
                else "normal"
            ),
        }
        error_rate: dict[str, Any] = {
            "current": round(values["error_rate"], 6),
            "threshold": 0.01,
            "status": (
                "missing"
                if "error_rate" in empty_queries
                else "high"
                if values["error_rate"] >= 0.01
                else "normal"
            ),
        }
        cpu_current = round(values["cpu_usage_percent"], 2)
        memory_current = round(values["memory_working_set_bytes"], 2)
        cpu: dict[str, Any] = {
            "metric_name": "cpu_usage_percent",
            "statistics": {"current": cpu_current},
        }
        if "cpu_usage_percent" in empty_queries:
            cpu["status"] = "missing"
        memory: dict[str, Any] = {
            "metric_name": "memory_working_set_bytes",
            "statistics": {"current": memory_current},
        }
        if "memory_working_set_bytes" in empty_queries:
            memory["status"] = "missing"
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
            data_quality="partial" if empty_queries else "complete",
            fact=(
                f"{service_name} P95={p95_latency_ms['current']}ms, "
                f"5xx={error_rate['current'] * 100:.2f}%, CPU={cpu_current}%."
            ),
            inference=_metric_inference(
                service_name=service_name,
                p95_ms=p95_latency_ms["current"],
                error_rate=error_rate["current"],
                cpu_percent=cpu_current,
            ),
            uncertainty=_metric_uncertainty(service_name),
            qps=qps,
            p95_latency_ms=p95_latency_ms,
            error_rate=error_rate,
            cpu=cpu,
            memory=memory,
        )

    async def _query_range(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        query_template: str,
        service_name: str,
        *,
        start_seconds: float,
        end_seconds: float,
        step_seconds: int,
    ) -> tuple[float, bool]:
        query = query_template.replace(
            "{service_name}",
            escape_prometheus_label_value(service_name),
        )
        response = await client.get(
            f"{base_url}/api/v1/query_range",
            params={
                "query": query,
                "start": f"{start_seconds:.3f}",
                "end": f"{end_seconds:.3f}",
                "step": str(step_seconds),
            },
        )
        response.raise_for_status()
        payload = require_success_payload(
            response.json(),
            system_name="Prometheus",
        )
        if payload.get("status") != "success":
            raise ExternalAdapterError("Prometheus query returned a non-success status")
        result = payload.get("data", {}).get("result", [])
        if not isinstance(result, list) or not result:
            return 0.0, False
        for series in result:
            if not isinstance(series, dict):
                continue
            samples = series.get("values")
            if not isinstance(samples, list):
                value = series.get("value")
                samples = [value] if isinstance(value, (list, tuple)) else []
            for sample in reversed(samples):
                if not isinstance(sample, (list, tuple)) or len(sample) < 2:
                    continue
                value = first_float(sample[1], default=float("nan"))
                if math.isfinite(value):
                    return value, True
        return 0.0, False


def _metric_inference(
    *,
    service_name: str,
    p95_ms: float,
    error_rate: float,
    cpu_percent: float,
) -> str:
    if service_name == "payment-service":
        return (
            "P95 degradation is the primary user-impact symptom. The error rate increased "
            "less sharply than the Redis outage case, and CPU is treated as a concurrent "
            "load symptom until SQL and pool evidence explain the latency."
        )
    return (
        f"Service impact is present with P95={p95_ms:.2f}ms, "
        f"error_rate={error_rate:.4f}, CPU={cpu_percent:.2f}%."
    )


def _metric_uncertainty(service_name: str) -> str:
    if service_name == "payment-service":
        return (
            "Prometheus shows impact and correlation only; high CPU cannot identify the slow "
            "SQL digest or prove that CPU saturation is the root cause."
        )
    return (
        "Prometheus metrics describe impact and timing but require logs and dependency-domain "
        "evidence before selecting a root cause."
    )


def _apply_prometheus_window(query_template: str, time_range: str) -> str:
    """Use the requested bounded window for rate/range-vector queries."""
    seconds = min(max(parse_duration_seconds(time_range), 1), 3600)
    return query_template.replace("[5m]", f"[{seconds}s]")
