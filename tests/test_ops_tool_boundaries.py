"""Tests for operational tool adapter boundaries."""

from __future__ import annotations

import pytest

from app.config import config
from app.tools.metrics_tool import QueryMetricsTool
from app.tools.ops_tool import QueryK8sStatusTool


class UnconfiguredKubernetesAdapter:
    configured = False


class UnconfiguredPrometheusAdapter:
    configured = False


class ConfiguredPrometheusAdapter:
    configured = True

    def __init__(self):
        self.calls = 0

    async def query_service_metrics(self, service_name: str, time_range: str, interval: str):
        self.calls += 1
        return {
            "status": "success",
            "source": "prometheus",
            "service_name": service_name,
            "time_range": time_range,
            "interval": interval,
            "summary": "prometheus ok",
            "signals": {"qps": 1},
        }


class FailedMCPTool:
    name = "query_cpu_metrics"

    async def ainvoke(self, _input_args: dict):
        return {"status": "failed", "error_type": "server_error", "error_message": "down"}


@pytest.mark.asyncio
async def test_query_metrics_does_not_synthesize_mock_success(monkeypatch) -> None:
    monkeypatch.setattr(config, "aiops_mock_fallback_enabled", True)
    tool = QueryMetricsTool([], prometheus_adapter=UnconfiguredPrometheusAdapter())

    result = await tool.arun({"service_name": "order-service", "time_range": "10m"})

    assert result.status == "failed"
    assert result.output["source"] == "metrics"
    assert result.output["error_type"] == "not_configured"


@pytest.mark.asyncio
async def test_query_k8s_status_does_not_synthesize_mock_success(monkeypatch) -> None:
    monkeypatch.setattr(config, "aiops_mock_fallback_enabled", True)
    tool = QueryK8sStatusTool(k8s_adapter=UnconfiguredKubernetesAdapter())

    result = await tool.arun({"service_name": "inventory-service", "time_range": "10m"})

    assert result.status == "failed"
    assert result.output["source"] == "kubernetes"
    assert result.output["error_type"] == "not_configured"
    assert result.output["pods"] == []
    assert result.output["events"] == []


@pytest.mark.asyncio
async def test_query_metrics_falls_back_to_prometheus_after_partial_mcp_failure() -> None:
    prometheus = ConfiguredPrometheusAdapter()
    tool = QueryMetricsTool([FailedMCPTool()], prometheus_adapter=prometheus)

    result = await tool.arun({"service_name": "order-service"})

    assert result.status == "success"
    assert result.output["source"] == "prometheus"
    assert prometheus.calls == 1
