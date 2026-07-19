"""Boundary tests for CLS-backed log queries."""

from __future__ import annotations

import pytest

from app.tools.logs_tool import QueryLogsTool


class FakeMCPTool:
    def __init__(self, name: str, handler):
        self.name = name
        self._handler = handler

    async def ainvoke(self, input_args: dict):
        return self._handler(input_args)


class UnconfiguredAdapter:
    configured = False


class ConfiguredAdapter:
    configured = True

    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    async def search_logs(self, *_args):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_cls_query_uses_requested_time_range_and_marks_mock_source() -> None:
    captured: dict[str, int] = {}

    def topic_handler(_: dict) -> dict:
        return {
            "status": "success",
            "source": "mock",
            "synthetic": True,
            "topics": [{"topic_id": "topic-001"}],
        }

    def timestamp_handler(_: dict) -> int:
        return 2_000_000

    def log_handler(input_args: dict) -> dict:
        captured.update(
            {
                "start_time": input_args["start_time"],
                "end_time": input_args["end_time"],
            }
        )
        return {
            "status": "success",
            "source": "mock",
            "synthetic": True,
            "total": 0,
            "logs": [],
        }

    tool = QueryLogsTool(
        [
            FakeMCPTool("search_topic_by_service_name", topic_handler),
            FakeMCPTool("get_current_timestamp", timestamp_handler),
            FakeMCPTool("search_log", log_handler),
        ],
        log_gateway=UnconfiguredAdapter(),
        loki_adapter=UnconfiguredAdapter(),
    )

    result = await tool.arun(
        {
            "service_name": "order-service",
            "time_range": "30m",
            "query": "ERROR",
        }
    )

    assert result.status == "success"
    assert captured == {"start_time": 200_000, "end_time": 2_000_000}
    assert result.output["source"] == "mock"
    assert result.output["synthetic"] is True
    assert result.output["time_range"] == "30m"


@pytest.mark.asyncio
async def test_cls_topic_lookup_failure_is_not_treated_as_unconfigured() -> None:
    def topic_handler(_: dict) -> dict:
        return {
            "status": "failed",
            "source": "mock",
            "synthetic": True,
            "error_message": "topic lookup failed",
        }

    tool = QueryLogsTool(
        [FakeMCPTool("search_topic_by_service_name", topic_handler)],
        log_gateway=UnconfiguredAdapter(),
        loki_adapter=UnconfiguredAdapter(),
    )

    result = await tool.arun({"service_name": "order-service"})

    assert result.status == "failed"
    assert result.output["source"] == "mcp_cls"
    assert result.output["error_message"] == "topic lookup failed"


@pytest.mark.asyncio
async def test_loki_failure_falls_back_to_log_gateway() -> None:
    loki = ConfiguredAdapter(error=ConnectionError("loki internal host"))
    gateway = ConfiguredAdapter(
        result={
            "status": "success",
            "source": "log_gateway",
            "logs": {"total": 1, "logs": [{"message": "ERROR timeout"}]},
            "summary": "gateway ok",
        }
    )
    tool = QueryLogsTool([], log_gateway=gateway, loki_adapter=loki)

    result = await tool.arun({"service_name": "order-service", "query": "ERROR"})

    assert result.status == "success"
    assert result.output["source"] == "log_gateway"
    assert loki.calls == 1
    assert gateway.calls == 1
    assert "loki internal host" not in str(result.output)
