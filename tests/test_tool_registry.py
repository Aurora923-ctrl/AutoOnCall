"""Tests for the AIOps Tool Registry."""

from __future__ import annotations

import pytest

from app.config import config
from app.tools.base import AIOpsTool
from app.tools.registry import create_default_tool_registry


class FakeAsyncTool:
    def __init__(self, name: str, output: dict):
        self.name = name
        self.description = f"fake {name}"
        self.output = output

    async def ainvoke(self, input_args: dict):
        return {"input_args": input_args, **self.output}


class FailingAsyncTool:
    def __init__(self, name: str):
        self.name = name
        self.description = f"failing {name}"

    async def ainvoke(self, input_args: dict):
        raise RuntimeError(f"{self.name} unavailable")


class MutableContractTool(AIOpsTool):
    name = "mutable_contract"
    description = "tool with nested class-level defaults"
    input_schema = {
        "type": "object",
        "properties": {"service_name": {"type": "string"}},
    }
    output_schema = {"properties": {"status": {"type": "string"}}}
    data_sources = ["source-a"]

    async def _call(self, input_args: dict):
        return {"status": "ok"}


def test_registry_registers_standard_aiops_tools() -> None:
    registry = create_default_tool_registry([])
    names = {item["name"] for item in registry.list_tools()}

    assert "query_metrics" in names
    assert "query_logs" in names
    assert "query_service_context" in names
    assert "query_deploy_history" in names
    assert "query_redis_status" in names
    assert "search_runbook" in names
    assert "suggest_remediation" in names


def test_registry_exposes_auditable_tool_contracts() -> None:
    registry = create_default_tool_registry([])
    contracts = {item["name"]: item for item in registry.list_tools()}

    redis_contract = contracts["query_redis_status"]
    metrics_contract = contracts["query_metrics"]
    remediation_contract = contracts["suggest_remediation"]

    assert redis_contract["input_schema"]["properties"]["service_name"]["type"] == "string"
    assert redis_contract["read_only"] is True
    assert redis_contract["risk_level"] == "low"
    assert "Redis INFO" in redis_contract["data_sources"]
    assert "structured unavailable payload" in redis_contract["degradation_strategy"]
    assert redis_contract["retry_policy"]["max_attempts"] == 1

    assert "mock" not in {source.lower() for source in metrics_contract["data_sources"]}
    assert "synthesizing metric evidence" in metrics_contract["degradation_strategy"]

    assert remediation_contract["risk_level"] == "medium"
    assert remediation_contract["read_only"] is True
    assert "diagnosis evidence" in remediation_contract["data_sources"]


@pytest.mark.asyncio
async def test_query_metrics_uses_mcp_like_tools_when_available() -> None:
    registry = create_default_tool_registry(
        [
            FakeAsyncTool(
                "query_cpu_metrics",
                {"metric_name": "cpu_usage_percent", "statistics": {"max": 88}},
            ),
            FakeAsyncTool(
                "query_memory_metrics",
                {"metric_name": "memory_usage_percent", "statistics": {"max": 76}},
            ),
        ]
    )

    result = await registry.arun(
        "query_metrics",
        {"service_name": "order-service", "time_range": "10m", "interval": "1m"},
    )

    assert result.status == "success"
    assert result.read_only is True
    assert result.risk_level == "low"
    assert result.output["source"] == "mcp_monitor"
    assert result.output["cpu"]["metric_name"] == "cpu_usage_percent"
    assert result.output["memory"]["metric_name"] == "memory_usage_percent"
    assert result.output["source_detail"]["qps"] == "unavailable"
    assert "qps" not in result.output
    assert "p95_latency_ms" not in result.output
    assert "error_rate" not in result.output
    assert "synthetic_fields" not in result.output


@pytest.mark.asyncio
async def test_query_metrics_fails_partial_mcp_without_synthetic_backfill() -> None:
    registry = create_default_tool_registry(
        [
            FailingAsyncTool("query_cpu_metrics"),
            FakeAsyncTool(
                "query_memory_metrics",
                {"metric_name": "memory_usage_percent", "statistics": {"max": 76}},
            ),
        ]
    )

    result = await registry.arun(
        "query_metrics",
        {"service_name": "order-service", "time_range": "10m", "interval": "1m"},
    )

    assert result.status == "failed"
    assert result.output["source"] == "mcp_monitor_mixed"
    assert result.output["source_detail"]["cpu"] == "unavailable"
    assert result.output["source_detail"]["memory"] == "mcp_monitor"
    assert result.output["available_metrics"]["memory"]["metric_name"] == "memory_usage_percent"
    assert result.output["partial_errors"][0]["tool_name"] == "query_cpu_metrics"
    assert "synthetic backfill is disabled" in result.output["summary"]


@pytest.mark.asyncio
async def test_query_tools_clamp_unbounded_inputs() -> None:
    registry = create_default_tool_registry([])

    metrics = await registry.arun(
        "query_metrics",
        {"service_name": "order-service", "time_range": "999h", "interval": "999m"},
    )
    logs = await registry.arun(
        "query_logs",
        {"service_name": "order-service", "time_range": "999h", "limit": 99999},
    )
    assert metrics.input_args["time_range"] == "1h"
    assert metrics.input_args["interval"] == "5m"
    assert logs.input_args["time_range"] == "1h"
    assert logs.input_args["limit"] == 200


def test_tool_contract_defaults_are_isolated_per_instance() -> None:
    first = MutableContractTool()
    second = MutableContractTool()

    first.input_schema["properties"]["mutated"] = {"type": "boolean"}
    first.output_schema["properties"]["mutated"] = {"type": "boolean"}
    first.data_sources.append("mutated-source")

    assert "mutated" not in second.input_schema["properties"]
    assert "mutated" not in second.output_schema["properties"]
    assert second.data_sources == ["source-a"]
    assert "mutated" not in MutableContractTool.input_schema["properties"]
    assert "mutated-source" not in MutableContractTool.data_sources


@pytest.mark.asyncio
async def test_query_redis_status_does_not_mock_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(config, "aiops_mock_fallback_enabled", True)
    monkeypatch.setattr(config, "redis_url", "")
    monkeypatch.setattr(config, "redis_host", "")
    monkeypatch.setattr(config, "redis_instances", "")
    registry = create_default_tool_registry([])

    result = await registry.arun(
        "query_redis_status",
        {"service_name": "order-service", "time_range": "10m"},
    )

    assert result.status == "failed"
    assert result.tool_name == "query_redis_status"
    assert result.output["source"] == "redis_info"
    assert result.output["error_type"] == "not_configured"
    assert "summary" in result.output
