"""Tests for the AIOps Tool Registry."""

import pytest

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


def test_registry_registers_standard_aiops_tools() -> None:
    registry = create_default_tool_registry([])
    names = {item["name"] for item in registry.list_tools()}

    assert "query_alerts" in names
    assert "query_metrics" in names
    assert "query_logs" in names
    assert "query_traces" in names
    assert "query_service_context" in names
    assert "query_deploy_history" in names
    assert "query_message_queue_status" in names
    assert "query_redis_status" in names
    assert "search_runbook" in names
    assert "suggest_remediation" in names


def test_registry_exposes_auditable_tool_contracts() -> None:
    registry = create_default_tool_registry([])
    contracts = {item["name"]: item for item in registry.list_tools()}

    redis_contract = contracts["query_redis_status"]
    remediation_contract = contracts["suggest_remediation"]
    queue_contract = contracts["query_message_queue_status"]

    assert redis_contract["input_schema"]["properties"]["service_name"]["type"] == "string"
    assert redis_contract["read_only"] is True
    assert redis_contract["risk_level"] == "low"
    assert "Redis INFO" in redis_contract["data_sources"]
    assert "结构化" in redis_contract["degradation_strategy"]
    assert redis_contract["retry_policy"]["max_attempts"] == 1

    assert remediation_contract["risk_level"] == "medium"
    assert remediation_contract["read_only"] is True
    assert "diagnosis evidence" in remediation_contract["data_sources"]

    assert "Redpanda Admin API" in queue_contract["data_sources"]
    assert "Kafka metadata" not in queue_contract["data_sources"]


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


@pytest.mark.asyncio
async def test_query_metrics_marks_partial_mcp_failures_without_failed_evidence() -> None:
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

    assert result.status == "success"
    assert result.output["source"] == "mcp_monitor"
    assert result.output["cpu"]["metric_name"] == "cpu_usage_percent"
    assert result.output["memory"]["metric_name"] == "memory_usage_percent"
    assert result.output["partial_errors"][0]["tool_name"] == "query_cpu_metrics"


@pytest.mark.asyncio
async def test_query_redis_status_returns_structured_mock_output() -> None:
    registry = create_default_tool_registry([])

    result = await registry.arun(
        "query_redis_status",
        {"service_name": "order-service", "time_range": "10m"},
    )

    assert result.status == "success"
    assert result.tool_name == "query_redis_status"
    assert result.output["connected_clients"] <= result.output["maxclients"]
    assert "summary" in result.output
