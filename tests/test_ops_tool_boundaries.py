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
