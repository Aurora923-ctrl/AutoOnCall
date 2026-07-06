"""Tracing backend adapters for Jaeger/Tempo evidence."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import config
from app.integrations.base import adapter_success, bearer_headers, require_config


class TracingAdapter:
    """Read trace summaries from Jaeger Query API or Tempo TraceQL search."""

    def __init__(
        self,
        jaeger_url: str | None = None,
        tempo_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.jaeger_url = (jaeger_url if jaeger_url is not None else config.jaeger_base_url).rstrip(
            "/"
        )
        self.tempo_url = (tempo_url if tempo_url is not None else config.tempo_base_url).rstrip("/")
        self.token = config.jaeger_bearer_token
        self.timeout_seconds = config.jaeger_timeout_seconds
        self.tempo_token = config.tempo_bearer_token
        self.tempo_timeout_seconds = config.tempo_timeout_seconds
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.jaeger_url or self.tempo_url)

    @property
    def source_name(self) -> str:
        if self.jaeger_url:
            return "jaeger"
        if self.tempo_url:
            return "tempo"
        return "tracing"

    async def query_service_traces(
        self,
        service_name: str,
        lookback: str = "1h",
        limit: int = 20,
    ) -> dict[str, Any]:
        if self.jaeger_url:
            return await self._query_jaeger_service_traces(service_name, lookback, limit)
        return await self._query_tempo_service_traces(service_name, limit)

    async def _query_jaeger_service_traces(
        self,
        service_name: str,
        lookback: str,
        limit: int,
    ) -> dict[str, Any]:
        base_url = require_config(self.jaeger_url, "JAEGER_BASE_URL")
        bounded_limit = min(max(int(limit), 1), 100)
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=bearer_headers(self.token),
            transport=self.transport,
        ) as client:
            response = await client.get(
                f"{base_url}/api/traces",
                params={"service": service_name, "lookback": lookback, "limit": bounded_limit},
            )
            response.raise_for_status()
            payload = response.json()

        traces = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(traces, list):
            traces = []
        summaries = [self._trace_summary(trace) for trace in traces[:bounded_limit]]
        error_spans = sum(item["error_span_count"] for item in summaries)
        slowest_us = max([item["duration_us"] for item in summaries], default=0)
        return adapter_success(
            source="jaeger",
            summary=(
                f"Jaeger 返回 {service_name} 最近 {len(summaries)} 条 trace，"
                f"error_spans={error_spans}, slowest_us={slowest_us}"
            ),
            signals={
                "trace_count": len(summaries),
                "error_span_count": error_spans,
                "slowest_duration_us": slowest_us,
            },
            raw={"trace_count": len(traces), "tempo_configured": bool(self.tempo_url)},
            service_name=service_name,
            backend="jaeger",
            tempo_configured=bool(self.tempo_url),
            traces=summaries,
        )

    async def _query_tempo_service_traces(
        self,
        service_name: str,
        limit: int,
    ) -> dict[str, Any]:
        base_url = require_config(self.tempo_url, "TEMPO_BASE_URL")
        bounded_limit = min(max(int(limit), 1), 100)
        query = f'{{ resource.service.name = "{_escape_traceql_string(service_name)}" }}'
        async with httpx.AsyncClient(
            timeout=self.tempo_timeout_seconds,
            headers=bearer_headers(self.tempo_token),
            transport=self.transport,
        ) as client:
            response = await client.get(
                f"{base_url}/api/search",
                params={"q": query, "limit": bounded_limit},
            )
            response.raise_for_status()
            payload = response.json()

        traces = payload.get("traces", []) if isinstance(payload, dict) else []
        if not isinstance(traces, list):
            traces = []
        summaries = [self._tempo_trace_summary(trace) for trace in traces[:bounded_limit]]
        error_spans = sum(item["error_span_count"] for item in summaries)
        slowest_us = max([item["duration_us"] for item in summaries], default=0)
        return adapter_success(
            source="tempo",
            summary=(
                f"Tempo 返回 {service_name} 最近 {len(summaries)} 条 trace，"
                f"error_spans={error_spans}, slowest_us={slowest_us}"
            ),
            signals={
                "trace_count": len(summaries),
                "error_span_count": error_spans,
                "slowest_duration_us": slowest_us,
            },
            raw={"trace_count": len(traces), "query": query},
            service_name=service_name,
            backend="tempo",
            tempo_configured=True,
            traces=summaries,
        )

    @staticmethod
    def _trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
        spans = trace.get("spans", []) if isinstance(trace, dict) else []
        processes = trace.get("processes", {}) if isinstance(trace, dict) else {}
        error_span_count = 0
        services: set[str] = set()
        for span in spans if isinstance(spans, list) else []:
            tags = span.get("tags", []) if isinstance(span, dict) else []
            if any(
                tag.get("key") == "error" and tag.get("value")
                for tag in tags
                if isinstance(tag, dict)
            ):
                error_span_count += 1
            process_id = span.get("processID") if isinstance(span, dict) else ""
            process = processes.get(process_id, {}) if isinstance(processes, dict) else {}
            service_name = process.get("serviceName") if isinstance(process, dict) else ""
            if service_name:
                services.add(str(service_name))
        return {
            "trace_id": str(trace.get("traceID") or ""),
            "span_count": len(spans) if isinstance(spans, list) else 0,
            "duration_us": int(trace.get("duration") or 0),
            "start_time_us": int(trace.get("startTime") or 0),
            "error_span_count": error_span_count,
            "services": sorted(services),
        }

    @staticmethod
    def _tempo_trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
        span_sets = trace.get("spanSets") or trace.get("spanSet") or []
        if isinstance(span_sets, dict):
            span_sets = [span_sets]
        spans: list[dict[str, Any]] = []
        if isinstance(span_sets, list):
            for span_set in span_sets:
                if not isinstance(span_set, dict):
                    continue
                items = span_set.get("spans") or []
                if isinstance(items, list):
                    spans.extend(item for item in items if isinstance(item, dict))

        services = {str(trace.get("rootServiceName") or "")}
        services.update(_tempo_span_service(span) for span in spans)
        services.discard("")
        return {
            "trace_id": str(trace.get("traceID") or trace.get("traceId") or ""),
            "span_count": len(spans),
            "duration_us": _tempo_duration_us(trace),
            "start_time_us": _tempo_start_time_us(trace),
            "error_span_count": sum(1 for span in spans if _tempo_span_has_error(span)),
            "services": sorted(services),
        }


def _tempo_duration_us(trace: dict[str, Any]) -> int:
    duration_ms = trace.get("durationMs")
    if duration_ms is not None:
        return int(float(duration_ms) * 1000)
    duration_us = trace.get("duration_us") or trace.get("duration")
    return int(float(duration_us or 0))


def _tempo_start_time_us(trace: dict[str, Any]) -> int:
    start_ns = trace.get("startTimeUnixNano")
    if start_ns is not None:
        return int(float(start_ns) / 1000)
    return int(float(trace.get("startTime") or 0))


def _tempo_span_service(span: dict[str, Any]) -> str:
    attributes = _tempo_span_attributes(span)
    return str(attributes.get("resource.service.name") or attributes.get("service.name") or "")


def _tempo_span_has_error(span: dict[str, Any]) -> bool:
    attributes = _tempo_span_attributes(span)
    status_code = str(attributes.get("status.code") or attributes.get("otel.status_code") or "")
    error_value = attributes.get("error")
    error_text = str(error_value).lower()
    return status_code.upper() == "ERROR" or error_value is True or error_text in {"true", "1"}


def _tempo_span_attributes(span: dict[str, Any]) -> dict[str, Any]:
    raw = span.get("attributes") or span.get("tags") or {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, list):
        return {}
    attributes: dict[str, Any] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        if key:
            attributes[key] = item.get("value")
    return attributes


def _escape_traceql_string(value: str) -> str:
    """Escape a value for a quoted TraceQL string literal."""
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')
