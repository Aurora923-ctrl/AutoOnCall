"""Tests for Executor evidence and tool-call audit records."""

import importlib
from typing import Any

import pytest

from app.agent.aiops import create_initial_aiops_state
from app.config import config
from app.models.plan import PlanStep
from app.tools.base import AIOpsTool, ToolExecutionResult
from app.tools.registry import ToolRegistry

executor_module = importlib.import_module("app.agent.aiops.executor")


class EmptyMCPClient:
    async def get_tools(self) -> list[Any]:
        return []


async def fake_get_mcp_client_with_retry() -> EmptyMCPClient:
    return EmptyMCPClient()


class FakeNamedTool:
    def __init__(self, name: str):
        self.name = name


def test_executor_llm_fallback_filters_unsafe_mcp_tools() -> None:
    safe_time_tool = FakeNamedTool("get_current_time")
    safe_knowledge_tool = FakeNamedTool("retrieve_knowledge")
    unsafe_mcp_tool = FakeNamedTool("delete_pod")

    filtered = executor_module._safe_fallback_tools(
        [safe_time_tool, safe_knowledge_tool, unsafe_mcp_tool]
    )

    assert [tool.name for tool in filtered] == ["get_current_time", "retrieve_knowledge"]


def test_executor_persistence_redacts_sensitive_tool_input_args() -> None:
    result = ToolExecutionResult(
        tool_name="query_logs",
        status="success",
        input_args={
            "service_name": "order-service",
            "api_token": "secret-token",
            "nested": {"password": "redis-password", "query": "ERROR"},
        },
        output={
            "summary": "ok token=summary-secret",
            "logs": [
                "Authorization: Bearer log-secret",
                {"message": "cookie=session-secret", "password": "raw-password"},
            ],
        },
    )

    persisted = executor_module._result_for_persistence(result)

    assert persisted.input_args["service_name"] == "order-service"
    assert persisted.input_args["api_token"] == "[REDACTED]"
    assert persisted.input_args["nested"]["password"] == "[REDACTED]"
    assert persisted.input_args["nested"]["query"] == "ERROR"
    assert persisted.output["summary"] == "ok token=[REDACTED]"
    assert persisted.output["logs"][0] == "Authorization: Bearer [REDACTED]"
    assert persisted.output["logs"][1]["message"] == "cookie=[REDACTED]"
    assert persisted.output["logs"][1]["password"] == "[REDACTED]"


def state_with_step(step: PlanStep) -> dict[str, Any]:
    state = create_initial_aiops_state(
        "diagnose order-service Redis timeout",
        session_id="executor-evidence-test",
    )
    state["current_plan"] = [step.model_dump(mode="json")]
    state["plan"] = [step.purpose]
    return state


@pytest.mark.asyncio
async def test_executor_registry_step_creates_evidence_and_tool_call_record(monkeypatch) -> None:
    monkeypatch.setattr(config, "aiops_mock_fallback_enabled", True)
    monkeypatch.setattr(config, "redis_url", "")
    monkeypatch.setattr(config, "redis_host", "")
    monkeypatch.setattr(config, "redis_instances", "")
    monkeypatch.setattr(
        executor_module,
        "get_mcp_client_with_retry",
        fake_get_mcp_client_with_retry,
    )
    step = PlanStep(
        step_id="s1",
        tool_name="query_redis_status",
        purpose="检查 Redis 连接数和 maxclients",
        input_args={"service_name": "order-service", "time_range": "10m"},
        expected_evidence="Redis connected_clients 是否接近 maxclients",
    )
    state = state_with_step(step)

    update = await executor_module.executor(state)

    assert update["current_plan"] == []
    assert update["plan"] == []
    assert update["past_steps"]
    assert update["executed_steps"][0]["status"] == "success"
    assert update["plan"] == []

    evidence = update["gathered_evidence"][0]
    assert evidence["source_tool"] == "query_redis_status"
    assert evidence["step_id"] == "s1"
    assert evidence["evidence_type"] == "redis"
    assert evidence["data_source"] == "mock"
    assert evidence["stance"] == "supporting"
    assert "Redis" in evidence["confidence_reason"]
    assert 0.45 <= evidence["confidence"] <= 0.55
    assert "connected_clients" in evidence["summary"]
    assert "来源=mock" in evidence["fact"]
    assert "支持当前根因假设" in evidence["inference"]
    assert "Mock 回退" in evidence["uncertainty"]
    assert "接入真实适配器" in evidence["next_step"]
    assert evidence["raw_data"]["status"] == "success"

    record = update["tool_call_records"][0]
    assert record["trace_id"] == state["trace_id"]
    assert record["incident_id"] == state["incident"]["incident_id"]
    assert record["step_id"] == "s1"
    assert record["tool_name"] == "query_redis_status"
    assert record["input_args"]["service_name"] == "order-service"
    assert "order-service" in record["input_summary"]
    assert record["data_source"] == "mock"
    assert "connected_clients" in record["output_summary"]
    assert record["risk_level"] == "low"
    assert record["read_only"] is True
    assert record["status"] == "success"
    assert record["error_message"] is None
    assert record["latency_ms"] >= 0


class FailingRedisTool(AIOpsTool):
    name = "query_redis_status"
    description = "failing redis test tool"
    risk_level = "low"
    read_only = True

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("redis backend unavailable")


def create_failing_registry(_: list[Any] | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(FailingRedisTool())
    return registry


@pytest.mark.asyncio
async def test_executor_failed_tool_creates_error_evidence_without_breaking_flow(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        executor_module,
        "get_mcp_client_with_retry",
        fake_get_mcp_client_with_retry,
    )
    monkeypatch.setattr(
        executor_module,
        "create_default_tool_registry",
        create_failing_registry,
    )
    step = PlanStep(
        step_id="s2",
        tool_name="query_redis_status",
        purpose="检查 Redis 状态",
        input_args={"service_name": "order-service"},
        expected_evidence="Redis 状态证据",
    )
    state = state_with_step(step)

    update = await executor_module.executor(state)

    assert update["past_steps"]
    assert update["executed_steps"][0]["status"] == "failed"
    assert update["errors"]

    evidence = update["gathered_evidence"][0]
    assert evidence["source_tool"] == "query_redis_status"
    assert evidence["evidence_type"] == "redis"
    assert evidence["stance"] == "unknown"
    assert "工具失败" in evidence["confidence_reason"]
    assert evidence["confidence"] == 0.1
    assert "调用失败" in evidence["summary"]
    assert "证据缺口" in evidence["inference"]
    assert evidence["raw_data"]["status"] == "failed"
    assert evidence["raw_data"]["error_message"] == "redis backend unavailable"

    record = update["tool_call_records"][0]
    assert record["status"] == "failed"
    assert record["error_message"] == "redis backend unavailable"
    assert record["output"] is None


class StructuredFailingRedisTool(AIOpsTool):
    name = "query_redis_status"
    description = "structured failing redis test tool"
    risk_level = "low"
    read_only = True

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "failed",
            "error_message": "redis adapter returned no usable data",
            "summary": "Redis 查询失败",
        }


def create_structured_failing_registry(_: list[Any] | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(StructuredFailingRedisTool())
    return registry


@pytest.mark.asyncio
async def test_executor_treats_structured_failure_payload_as_failed_evidence(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        executor_module,
        "get_mcp_client_with_retry",
        fake_get_mcp_client_with_retry,
    )
    monkeypatch.setattr(
        executor_module,
        "create_default_tool_registry",
        create_structured_failing_registry,
    )
    step = PlanStep(
        step_id="s3",
        tool_name="query_redis_status",
        purpose="检查 Redis 状态",
        input_args={"service_name": "order-service"},
        expected_evidence="Redis 状态证据",
    )
    state = state_with_step(step)

    update = await executor_module.executor(state)

    evidence = update["gathered_evidence"][0]
    record = update["tool_call_records"][0]

    assert update["executed_steps"][0]["status"] == "failed"
    assert evidence["confidence"] == 0.1
    assert evidence["raw_data"]["status"] == "failed"
    assert record["status"] == "failed"
    assert record["error_message"] == "redis adapter returned no usable data"


@pytest.mark.asyncio
async def test_executor_manual_step_is_wrapped_as_structured_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        executor_module,
        "get_mcp_client_with_retry",
        fake_get_mcp_client_with_retry,
    )

    async def fake_llm_executor(task: str, all_tools: list[Any]) -> str:
        return f"人工分析已完成: {task[:20]}"

    monkeypatch.setattr(executor_module, "_execute_with_llm_tools", fake_llm_executor)

    step = PlanStep(
        step_id="s4",
        tool_name="manual_analysis",
        purpose="人工复核 Redis 连接数趋势",
        input_args={"service_name": "order-service"},
        expected_evidence="人工复核结论",
    )
    state = state_with_step(step)

    update = await executor_module.executor(state)

    assert update["current_plan"] == []
    assert update["plan"] == []
    assert update["executed_steps"][0]["status"] == "success"
    assert update["gathered_evidence"][0]["source_tool"] == "manual_analysis"
    assert update["gathered_evidence"][0]["confidence"] == 0.35
    assert update["gathered_evidence"][0]["raw_data"]["metadata"]["execution_path"] == (
        "manual_analysis"
    )
    assert update["warnings"]
    assert "人工分析兜底路径" in update["warnings"][0]
    assert update["tool_call_records"][0]["tool_name"] == "manual_analysis"
    assert update["tool_call_records"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_executor_unregistered_tool_fallback_is_failed_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        executor_module,
        "get_mcp_client_with_retry",
        fake_get_mcp_client_with_retry,
    )

    async def fake_llm_executor(task: str, all_tools: list[Any]) -> str:
        return "legacy fallback observation"

    monkeypatch.setattr(executor_module, "_execute_with_llm_tools", fake_llm_executor)

    step = PlanStep(
        step_id="s5",
        tool_name="query_unregistered_system",
        purpose="检查未注册系统",
        input_args={"service_name": "order-service"},
        expected_evidence="未注册系统证据",
    )
    state = state_with_step(step)

    update = await executor_module.executor(state)

    assert update["past_steps"][0][1] == "legacy fallback observation"
    assert update["executed_steps"][0]["status"] == "failed"
    assert update["errors"]

    evidence = update["gathered_evidence"][0]
    assert evidence["source_tool"] == "query_unregistered_system"
    assert evidence["data_source"] == "failed"
    assert evidence["confidence"] == 0.1
    assert evidence["raw_data"]["status"] == "failed"
    assert evidence["raw_data"]["error_message"]
    assert evidence["raw_data"]["metadata"]["execution_path"] == "llm_toolnode_fallback"

    assert update["warnings"]
    assert "LLM ToolNode 兜底路径" in update["warnings"][0]

    record = update["tool_call_records"][0]
    assert record["tool_name"] == "query_unregistered_system"
    assert record["status"] == "failed"
    assert record["error_message"]
    assert record["output"]["structured_tool_registered"] is False
    assert record["output"]["fallback_reason"] == ("structured_tool_not_registered")
