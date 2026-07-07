"""Standard metrics tool backed by MCP monitor tools or Prometheus."""

from __future__ import annotations

from typing import Any

from app.integrations.base import adapter_failure, adapter_not_configured
from app.integrations.prometheus import PrometheusMetricsAdapter
from app.tools.base import (
    AIOpsTool,
    clamp_duration,
    invoke_langchain_tool,
    is_failed_tool_output,
    tool_map,
)


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
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    timeout_seconds = 12.0
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
        if mcp_result is not None:
            return mcp_result

        prometheus_result = await self._call_prometheus(service_name, time_range, interval)
        if prometheus_result is not None:
            return prometheus_result

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
        cpu_output = None if is_failed_tool_output(cpu_result) else cpu_result
        memory_output = None if is_failed_tool_output(memory_result) else memory_result
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
                "cpu": "mcp_monitor" if cpu_output else "unavailable",
                "memory": "mcp_monitor" if memory_output else "unavailable",
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
        output = {
            "service_name": service_name,
            "interval": interval,
            "source": "mcp_monitor",
            "cpu": cpu,
            "memory": memory,
            "source_detail": {
                "cpu": "mcp_monitor",
                "memory": "mcp_monitor",
                "qps": "unavailable",
                "p95_latency_ms": "unavailable",
                "error_rate": "unavailable",
            },
            "summary": f"{service_name} metrics_source=mcp_monitor, available_metrics=cpu,memory",
        }
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
            return {"status": "failed", "error_message": str(exc)}

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

    @staticmethod
    def _mcp_metric_error(result: Any) -> str:
        if isinstance(result, dict):
            return str(
                result.get("error_message")
                or result.get("error")
                or result.get("summary")
                or result.get("message")
                or "no usable MCP metric output"
            )
        return "no usable MCP metric output"
