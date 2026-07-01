"""Standard metrics tool backed by MCP monitor tools with mock fallback."""

from __future__ import annotations

from typing import Any

from app.config import config
from app.integrations.base import adapter_failure, adapter_not_configured
from app.integrations.prometheus import PrometheusMetricsAdapter
from app.tools.base import AIOpsTool, invoke_langchain_tool, is_failed_tool_output, tool_map


class QueryMetricsTool(AIOpsTool):
    """Query service metrics through monitor MCP tools or mock data."""

    name = "query_metrics"
    description = "查询服务 QPS、P95、错误率、CPU 和内存等监控指标"
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
    data_sources = ["MCP monitor", "Prometheus", "mock"]
    degradation_strategy = (
        "优先使用 MCP/Prometheus；单项指标失败时保留 partial_errors 并用 mock 指标补齐可解释证据"
    )

    def __init__(
        self,
        langchain_tools: list[Any] | None = None,
        prometheus_adapter: PrometheusMetricsAdapter | None = None,
    ):
        self._allow_adapter_failure_fallback = prometheus_adapter is None
        self._tools = tool_map(langchain_tools)
        self._prometheus = prometheus_adapter or PrometheusMetricsAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        interval = input_args.get("interval") or "1m"
        time_range = input_args.get("time_range", "10m")

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
        partial_errors = []
        if is_failed_tool_output(cpu_result):
            partial_errors.append(
                {
                    "tool_name": "query_cpu_metrics",
                    "error_message": cpu_result.get("error_message", "unknown error"),
                }
            )
        if is_failed_tool_output(memory_result):
            partial_errors.append(
                {
                    "tool_name": "query_memory_metrics",
                    "error_message": memory_result.get("error_message", "unknown error"),
                }
            )

        if cpu_output or memory_output:
            output = self._build_monitor_output(
                service_name=service_name,
                time_range=time_range,
                interval=interval,
                source="mcp_monitor",
                cpu=cpu_output or self._mock_cpu(service_name),
                memory=memory_output or self._mock_memory(service_name),
                partial_errors=partial_errors,
            )
            return output

        prometheus_result = await self._call_prometheus(service_name, time_range, interval)
        if prometheus_result and not is_failed_tool_output(prometheus_result):
            return prometheus_result
        if (
            prometheus_result
            and config.aiops_mock_fallback_enabled
            and self._allow_adapter_failure_fallback
        ):
            partial_errors.append(
                {
                    "tool_name": "prometheus",
                    "error_message": prometheus_result.get("error_message", "unknown error"),
                }
            )
        elif prometheus_result:
            return prometheus_result

        if not config.aiops_mock_fallback_enabled and not cpu_output and not memory_output:
            if prometheus_result:
                return prometheus_result
            return adapter_not_configured(
                "metrics",
                required_config="PROMETHEUS_BASE_URL or MCP monitor tools",
                summary_prefix="监控指标查询不可用",
                service_name=service_name,
                time_range=time_range,
                interval=interval,
            )

        return self._build_monitor_output(
            service_name=service_name,
            time_range=time_range,
            interval=interval,
            source="mock",
            cpu=self._mock_cpu(service_name),
            memory=self._mock_memory(service_name),
            partial_errors=partial_errors,
        )

    def _build_monitor_output(
        self,
        *,
        service_name: str,
        time_range: str,
        interval: str,
        source: str,
        cpu: Any,
        memory: Any,
        partial_errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build the stable metrics payload shared by MCP and mock paths."""
        output = {
            "service_name": service_name,
            "time_range": time_range,
            "interval": interval,
            "source": source,
            "qps": {"current": 1280, "baseline": 900, "trend": "up"},
            "p95_latency_ms": {"current": 3250, "threshold": 1000, "status": "high"},
            "error_rate": {"current": 0.082, "threshold": 0.01, "status": "high"},
            "cpu": cpu,
            "memory": memory,
        }
        if partial_errors:
            output["partial_errors"] = partial_errors
        output["summary"] = (
            f"{service_name} P95={output['p95_latency_ms']['current']}ms, "
            f"5xx={output['error_rate']['current'] * 100:.2f}%, "
            f"metrics_source={output['source']}"
        )
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
                summary_prefix="Prometheus 查询失败",
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

    @staticmethod
    def _mock_cpu(service_name: str) -> dict[str, Any]:
        return {
            "service_name": service_name,
            "metric_name": "cpu_usage_percent",
            "statistics": {"avg": 72.5, "max": 91.2, "p95": 88.6, "spike_detected": True},
            "alert_info": {"triggered": True, "threshold": 80.0, "message": "CPU 使用率超过阈值"},
        }

    @staticmethod
    def _mock_memory(service_name: str) -> dict[str, Any]:
        return {
            "service_name": service_name,
            "metric_name": "memory_usage_percent",
            "statistics": {"avg": 68.1, "max": 79.4, "p95": 76.8, "memory_pressure": True},
            "alert_info": {"triggered": True, "threshold": 70.0, "message": "内存存在压力"},
        }
