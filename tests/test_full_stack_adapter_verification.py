"""Tests for full-stack adapter verification script logic."""

import importlib.util
from pathlib import Path

import pytest

from app.tools.base import ToolExecutionResult

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "sandbox" / "verify_full_stack_adapters.py"
SPEC = importlib.util.spec_from_file_location("verify_full_stack_adapters", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
verify_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_module)
verify_adapters = verify_module.verify_adapters


class FakeRegistry:
    def __init__(self, sources: dict[str, str]):
        self.sources = sources

    async def arun(self, name: str, input_args: dict):
        source = self.sources.get(name, "mock")
        status = "failed" if source == "failed" else "success"
        output = {
            "source": source,
            "summary": f"{name} returned {source}",
            "signals": {
                "fixture": 1,
                "slow_query_count": 18,
                "pool_waiting": 6,
                "active_connections": 188,
            },
        }
        if source == "failed":
            output.update({"status": "failed", "error_message": "adapter down"})
        return ToolExecutionResult(
            tool_name=name,
            status=status,
            input_args=input_args,
            output=output,
            error_message=output.get("error_message"),
        )


@pytest.mark.asyncio
async def test_verify_adapters_passes_when_all_real_sources_match() -> None:
    registry = FakeRegistry(
        {
            "query_metrics": "prometheus",
            "query_logs": "loki",
            "query_service_context": "cmdb",
            "query_deploy_history": "deploy_history",
            "query_redis_status": "redis_info",
            "query_mysql_status": "mysql",
            "search_history_ticket": "ticket_api",
        }
    )

    payload = await verify_adapters(registry)

    assert payload["status"] == "passed"
    assert payload["failed_tools"] == []
    assert payload["missing_sources"] == []
    assert payload["not_integrated"] == []
    assert payload["passed_golden_chain_count"] == 2
    assert payload["golden_chains"]["redis_maxclients"]["passed"] is True
    assert payload["golden_chains"]["mysql_slow_query"]["passed"] is True
    assert payload["golden_chains"]["redis_maxclients"]["required_sources"] == [
        "redis_info",
        "prometheus",
        "loki",
        "ticket_api",
    ]


@pytest.mark.asyncio
async def test_verify_adapters_fails_when_mock_fallback_is_used() -> None:
    registry = FakeRegistry(
        {
            "query_metrics": "mock",
            "query_logs": "loki",
            "query_service_context": "cmdb",
            "query_deploy_history": "deploy_history",
            "query_redis_status": "redis_info",
            "query_mysql_status": "mysql",
            "search_history_ticket": "ticket_api",
        }
    )

    payload = await verify_adapters(registry)

    assert payload["status"] == "failed"
    assert payload["failed_tools"] == ["query_metrics"]
    assert payload["mock_fallback_detected"] is True
    assert payload["golden_chains"]["redis_maxclients"]["passed"] is False
    assert payload["golden_chains"]["mysql_slow_query"]["passed"] is False
    assert "query_metrics" in payload["golden_chains"]["redis_maxclients"]["failed_tools"]
