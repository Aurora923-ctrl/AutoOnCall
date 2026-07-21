"""Standard metrics tool backed by MCP monitor tools or Prometheus."""

from __future__ import annotations

from typing import Any

from app.integrations.base import (
    adapter_failure,
    adapter_not_configured,
    public_adapter_failure_message,
)
from app.integrations.prometheus import PrometheusMetricsAdapter
from app.tools.base import (
    AIOpsTool,
    ToolRetryPolicy,
    clamp_duration,
    invoke_langchain_tool,
    is_failed_tool_output,
    tool_map,
)
from app.utils.public_errors import public_exception_message


class QueryMetricsTool(AIOpsTool):
    """Query service metrics through monitor MCP tools or Prometheus."""

    name = "query_metrics"
    description = "Query service QPS, P95, error rate, CPU, and memory metrics."
    input_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string"},
            "time_range": {"type": "string", "default": "10m"},
            "interval": {"type": "string", "default": "1m"},
        },
        "required": ["service_name"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    timeout_seconds = 12.0
    retry_policy = ToolRetryPolicy(
        max_attempts=2,
        backoff_seconds=0.1,
        retry_on=["timeout", "connection_error", "server_error"],
    )
    data_sources = ["MCP monitor", "Prometheus"]
    degradation_strategy = (
        "Use MCP monitor tools or Prometheus when configured; otherwise return a structured "
        "unavailable or failed payload without synthesizing metric evidence."
    )

    def __init__(
        self,
        langchain_tools: list[Any] | None = None,
        prometheus_adapter: PrometheusMetricsAdapter | None = None,
    ):
        super().__init__()
        self._tools = tool_map(langchain_tools)
        self._prometheus = prometheus_adapter or PrometheusMetricsAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        interval = clamp_duration(input_args.get("interval"), default="1m", maximum_seconds=300)
        time_range = clamp_duration(
            input_args.get("time_range"),
            default="10m",
            maximum_seconds=3600,
        )
        input_args.update({"interval": interval, "time_range": time_range})

        mcp_result = await self._call_mcp_metrics(service_name, interval)
        if mcp_result is not None and not is_failed_tool_output(mcp_result):
            return mcp_result

        prometheus_result = await self._call_prometheus(service_name, time_range, interval)
        if prometheus_result is not None and not is_failed_tool_output(prometheus_result):
            return self._with_fallback_errors(prometheus_result, mcp_result)
        if prometheus_result is not None:
            return self._with_fallback_errors(prometheus_result, mcp_result)
        if mcp_result is not None:
            return mcp_result

        return adapter_not_configured(
            "metrics",
            required_config="PROMETHEUS_BASE_URL or MCP monitor tools",
            summary_prefix="Metrics query unavailable",
            service_name=service_name,
            time_range=time_range,
            interval=interval,
        )

    async def _call_mcp_metrics(self, service_name: str, interval: str) -> dict[str, Any] | None:
        if "query_cpu_metrics" not in self._tools and "query_memory_metrics" not in self._tools:
            return None

        cpu_result = await self._call_optional_mcp(
            "query_cpu_metrics",
            {"service_name": service_name, "interval": interval},
        )
        memory_result = await self._call_optional_mcp(
            "query_memory_metrics",
            {"service_name": service_name, "interval": interval},
        )
        cpu_output = cpu_result if self._usable_mcp_metric_output(cpu_result) else None
        memory_output = memory_result if self._usable_mcp_metric_output(memory_result) else None
        partial_errors = self._mcp_errors(cpu_result, memory_result)

        if cpu_output and memory_output:
            return self._build_mcp_output(
                service_name=service_name,
                interval=interval,
                cpu=cpu_output,
                memory=memory_output,
                partial_errors=partial_errors,
            )

        available_metrics: dict[str, Any] = {}
        if cpu_output:
            available_metrics["cpu"] = cpu_output
        if memory_output:
            available_metrics["memory"] = memory_output
        return {
            "status": "failed",
            "service_name": service_name,
            "interval": interval,
            "source": "mcp_monitor_mixed",
            "summary": (
                f"{service_name} metrics are only partially available; Prometheus or both MCP "
                "metric tools are required because synthetic backfill is disabled."
            ),
            "available_metrics": available_metrics,
            "partial_errors": partial_errors,
            "source_detail": {
                "cpu": self._mcp_metric_source(cpu_output) if cpu_output else "unavailable",
                "memory": self._mcp_metric_source(memory_output)
                if memory_output
                else "unavailable",
                "qps": "unavailable",
                "p95_latency_ms": "unavailable",
                "error_rate": "unavailable",
            },
        }

    @staticmethod
    def _build_mcp_output(
        *,
        service_name: str,
        interval: str,
        cpu: Any,
        memory: Any,
        partial_errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        cpu_source = QueryMetricsTool._mcp_metric_source(cpu)
        memory_source = QueryMetricsTool._mcp_metric_source(memory)
        synthetic_fields = [
            name
            for name, source in (("cpu", cpu_source), ("memory", memory_source))
            if source == "mock"
        ]
        source = (
            "mock"
            if len(synthetic_fields) == 2
            else "mcp_monitor_mixed"
            if synthetic_fields
            else "mcp_monitor"
        )
        output = {
            "service_name": service_name,
            "interval": interval,
            "source": source,
            "cpu": cpu,
            "memory": memory,
            "source_detail": {
                "cpu": cpu_source,
                "memory": memory_source,
                "qps": "unavailable",
                "p95_latency_ms": "unavailable",
                "error_rate": "unavailable",
            },
            "summary": f"{service_name} metrics_source={source}, available_metrics=cpu,memory",
        }
        if synthetic_fields:
            output["synthetic_fields"] = synthetic_fields
            output["source_quality"] = "fallback_only"
            output["evidence_origin"] = "mcp_mock:monitor"
            output["uncertainty"] = (
                "CPU/memory metrics come from the local synthetic MCP monitor and are not "
                "production observations."
            )
        if partial_errors:
            output["partial_errors"] = partial_errors
        return output

    async def _call_prometheus(
        self,
        service_name: str,
        time_range: str,
        interval: str,
    ) -> dict[str, Any] | None:
        if not self._prometheus.configured:
            return None
        try:
            return await self._prometheus.query_service_metrics(service_name, time_range, interval)
        except Exception as exc:
            return adapter_failure(
                "prometheus",
                exc,
                summary_prefix="Prometheus query failed",
                service_name=service_name,
                time_range=time_range,
                interval=interval,
            )

    async def _call_optional_mcp(self, tool_name: str, input_args: dict[str, Any]) -> Any:
        tool = self._tools.get(tool_name)
        if not tool:
            return None
        try:
            return await invoke_langchain_tool(tool, input_args)
        except Exception as exc:
            return {
                "status": "failed",
                "error_type": "mcp_error",
                "error_message": public_exception_message(exc),
            }

    def _mcp_errors(self, cpu_result: Any, memory_result: Any) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        self._append_mcp_error(errors, "query_cpu_metrics", cpu_result)
        self._append_mcp_error(errors, "query_memory_metrics", memory_result)
        return errors

    def _append_mcp_error(
        self,
        errors: list[dict[str, Any]],
        tool_name: str,
        result: Any,
    ) -> None:
        if result is None:
            errors.append({"tool_name": tool_name, "error_message": "MCP metric tool is missing"})
            return
        if is_failed_tool_output(result):
            errors.append({"tool_name": tool_name, "error_message": self._mcp_metric_error(result)})
            return
        if not isinstance(result, dict):
            errors.append(
                {
                    "tool_name": tool_name,
                    "error_message": "MCP metric tool returned a non-object payload",
                }
            )

    @staticmethod
    def _usable_mcp_metric_output(result: Any) -> bool:
        return isinstance(result, dict) and not is_failed_tool_output(result)

    @staticmethod
    def _mcp_metric_source(result: Any) -> str:
        if isinstance(result, dict) and (
            str(result.get("source") or "").lower() == "mock" or result.get("synthetic") is True
        ):
            return "mock"
        return "mcp_monitor"

    @staticmethod
    def _mcp_metric_error(result: Any) -> str:
        if isinstance(result, dict):
            error_type = str(result.get("error_type") or "adapter_error")
            return public_adapter_failure_message(error_type)
        return "no usable MCP metric output"

    @staticmethod
    def _with_fallback_errors(
        result: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not previous:
            return result
        output = dict(result)
        errors = list(output.get("fallback_errors") or [])
        errors.append(
            {
                "source": str(previous.get("source") or "mcp_monitor"),
                "error_message": public_adapter_failure_message(
                    str(previous.get("error_type") or "adapter_error")
                ),
            }
        )
        output["fallback_errors"] = errors
        return output
