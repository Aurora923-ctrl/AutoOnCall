"""Tracing query tool backed by Jaeger/Tempo adapters."""

from __future__ import annotations

from typing import Any

from app.integrations.tracing import TracingAdapter
from app.tools.base import AIOpsTool, clamp_duration, clamp_int
from app.tools.fallback import run_adapter_or_mock


class QueryTracesTool(AIOpsTool):
    name = "query_traces"
    description = "Query Jaeger/Tempo trace summaries to locate slow or error spans."
    risk_level = "low"
    read_only = True
    timeout_seconds = 10.0
    data_sources = ["Jaeger", "Tempo", "mock"]
    degradation_strategy = (
        "Use tracing backends when configured; otherwise return mock trace data when enabled "
        "or a structured unavailable payload when mock fallback is disabled."
    )

    def __init__(self, tracing_adapter: TracingAdapter | None = None):
        super().__init__()
        self._tracing_adapter = tracing_adapter or TracingAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        lookback = clamp_duration(
            input_args.get("lookback"),
            default="1h",
            maximum_seconds=86400,
        )
        limit = clamp_int(input_args.get("limit"), default=20, minimum=1, maximum=50)
        input_args.update({"lookback": lookback, "limit": limit})
        source = getattr(self._tracing_adapter, "source_name", "tracing")
        return await run_adapter_or_mock(
            configured=self._tracing_adapter.configured,
            adapter_call=lambda: self._tracing_adapter.query_service_traces(
                service_name, lookback, limit
            ),
            mock_call=lambda: {
                "status": "success",
                "source": "mock",
                "service_name": service_name,
                "traces": [],
                "signals": {"trace_count": 0, "error_span_count": 0},
                "summary": "mock tracing found no abnormal spans",
            },
            source=source,
            required_config="JAEGER_BASE_URL or TEMPO_BASE_URL",
            failure_summary_prefix="Tracing query failed",
            not_configured_summary_prefix="Tracing query unavailable",
            payload={"service_name": service_name, "lookback": lookback},
            unavailable_defaults={"traces": []},
        )
