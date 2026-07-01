"""Tracing query tool backed by Jaeger/Tempo adapters."""

from __future__ import annotations

from typing import Any

from app.config import config
from app.integrations.base import adapter_failure, adapter_not_configured
from app.integrations.tracing import TracingAdapter
from app.tools.base import AIOpsTool, clamp_duration, clamp_int


class QueryTracesTool(AIOpsTool):
    name = "query_traces"
    description = "查询 Jaeger/Tempo 调用链摘要，定位慢 span、错误 span 和下游传播路径"
    risk_level = "low"
    read_only = True
    timeout_seconds = 10.0
    data_sources = ["Jaeger", "Tempo", "mock"]
    degradation_strategy = (
        "Tracing 后端不可用时返回空调用链 mock 结果；关闭 mock 后返回结构化不可用结果"
    )

    def __init__(self, tracing_adapter: TracingAdapter | None = None):
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
        if self._tracing_adapter.configured:
            try:
                return await self._tracing_adapter.query_service_traces(
                    service_name, lookback, limit
                )
            except Exception as exc:
                source = getattr(self._tracing_adapter, "source_name", "tracing")
                payload = adapter_failure(
                    source,
                    exc,
                    summary_prefix="Tracing 查询失败",
                    service_name=service_name,
                    lookback=lookback,
                )
                payload.update({"traces": []})
                return payload
        if not config.aiops_mock_fallback_enabled:
            payload = adapter_not_configured(
                "tracing",
                required_config="JAEGER_BASE_URL or TEMPO_BASE_URL",
                summary_prefix="Tracing 查询不可用",
                service_name=service_name,
                lookback=lookback,
            )
            payload.update({"traces": []})
            return payload
        return {
            "status": "success",
            "source": "mock",
            "service_name": service_name,
            "traces": [],
            "signals": {"trace_count": 0, "error_span_count": 0},
            "summary": "mock tracing 未发现异常调用链",
        }
