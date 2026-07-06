"""Standard metrics tool backed by MCP monitor tools with mock fallback."""

from __future__ import annotations

from typing import Any

from app.config import config
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
        "优先使用 MCP/Prometheus；MCP 仅返回实际可得指标，mock 只在显式开启时补齐演示证据"
    )

    def __init__(
        self,
        langchain_tools: list[Any] | None = None,
        prometheus_adapter: PrometheusMetricsAdapter | None = None,
    ):
        super().__init__()
        self._allow_adapter_failure_fallback = prometheus_adapter is None
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
        partial_errors: list[dict[str, Any]] = []
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
            source = "mcp_monitor" if cpu_output and memory_output else "mcp_monitor_mixed"
            if not config.aiops_mock_fallback_enabled and source == "mcp_monitor_mixed":
                if cpu_output is None and not is_failed_tool_output(cpu_result):
                    partial_errors.append(
                        {
                            "tool_name": "query_cpu_metrics",
                            "error_message": self._mcp_metric_error(cpu_result),
                        }
                    )
                if memory_output is None and not is_failed_tool_output(memory_result):
                    partial_errors.append(
                        {
                            "tool_name": "query_memory_metrics",
                            "error_message": self._mcp_metric_error(memory_result),
                        }
                    )
                return self._build_partial_mcp_failure_output(
                    service_name=service_name,
                    time_range=time_range,
                    interval=interval,
                    cpu=cpu_output,
                    memory=memory_output,
                    partial_errors=partial_errors,
                )
            output = self._build_monitor_output(
                service_name=service_name,
                time_range=time_range,
                interval=interval,
                source=source,
                cpu=cpu_output or self._mock_cpu(service_name),
                memory=memory_output or self._mock_memory(service_name),
                partial_errors=partial_errors,
                include_synthetic_baseline=source != "mcp_monitor",
                source_detail={
                    "cpu": "mcp_monitor" if cpu_output else "mock_fallback",
                    "memory": "mcp_monitor" if memory_output else "mock_fallback",
                    "qps": "synthetic_demo_baseline"
                    if source != "mcp_monitor"
                    else "unavailable",
                    "p95_latency_ms": "synthetic_demo_baseline"
                    if source != "mcp_monitor"
                    else "unavailable",
                    "error_rate": "synthetic_demo_baseline"
                    if source != "mcp_monitor"
                    else "unavailable",
                },
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
            include_synthetic_baseline=True,
            source_detail={
                "cpu": "mock",
                "memory": "mock",
                "qps": "synthetic_demo_baseline",
                "p95_latency_ms": "synthetic_demo_baseline",
                "error_rate": "synthetic_demo_baseline",
            },
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
        source_detail: dict[str, str],
        include_synthetic_baseline: bool,
    ) -> dict[str, Any]:
        """Build the stable metrics payload shared by MCP and mock paths."""
        output = {
            "service_name": service_name,
            "time_range": time_range,
            "interval": interval,
            "source": source,
            "cpu": cpu,
            "memory": memory,
            "source_detail": source_detail,
        }
        if include_synthetic_baseline:
            output.update(
                {
                    "qps": {"current": 1280, "baseline": 900, "trend": "up"},
                    "p95_latency_ms": {"current": 3250, "threshold": 1000, "status": "high"},
                    "error_rate": {"current": 0.082, "threshold": 0.01, "status": "high"},
                }
            )
        synthetic_fields = [
            key for key, value in source_detail.items() if value == "synthetic_demo_baseline"
        ]
        if synthetic_fields:
            output["synthetic_fields"] = synthetic_fields
        if partial_errors:
            output["partial_errors"] = partial_errors
        output["summary"] = self._build_metrics_summary(output)
        return output

    @staticmethod
    def _build_metrics_summary(output: dict[str, Any]) -> str:
        service_name = str(output.get("service_name") or "unknown-service")
        source = str(output.get("source") or "unknown")
        p95 = output.get("p95_latency_ms")
        error_rate = output.get("error_rate")
        if isinstance(p95, dict) and isinstance(error_rate, dict):
            return (
                f"{service_name} P95={p95.get('current')}ms, "
                f"5xx={float(error_rate.get('current') or 0) * 100:.2f}%, "
                f"metrics_source={source}"
            )
        available = [
            name
            for name in ("cpu", "memory")
            if isinstance(output.get(name), dict) and output.get(name)
        ]
        return (
            f"{service_name} metrics_source={source}, "
            f"available_metrics={','.join(available) or 'none'}"
        )

    def _build_partial_mcp_failure_output(
        self,
        *,
        service_name: str,
        time_range: str,
        interval: str,
        cpu: Any,
        memory: Any,
        partial_errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return a failed payload when strict mode forbids synthetic metric backfill."""
        available_metrics = {}
        if cpu is not None:
            available_metrics["cpu"] = cpu
        if memory is not None:
            available_metrics["memory"] = memory
        return {
            "status": "failed",
            "service_name": service_name,
            "time_range": time_range,
            "interval": interval,
            "source": "mcp_monitor_mixed",
            "summary": (
                f"{service_name} 监控指标查询部分可用，但 mock fallback 已关闭，"
                "不能用合成指标补齐。"
            ),
            "available_metrics": available_metrics,
            "partial_errors": partial_errors,
            "source_detail": {
                "cpu": "mcp_monitor" if cpu is not None else "unavailable",
                "memory": "mcp_monitor" if memory is not None else "unavailable",
                "qps": "unavailable",
                "p95_latency_ms": "unavailable",
                "error_rate": "unavailable",
            },
        }

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
    def _mcp_metric_error(result: Any) -> str:
        if result is None:
            return "MCP metric tool is not configured"
        if isinstance(result, dict):
            return str(
                result.get("error_message")
                or result.get("error")
                or result.get("summary")
                or result.get("message")
                or "no usable MCP metric output"
            )
        return "no usable MCP metric output"

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
