"""Tests for the AIOps Tool Registry."""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.tools import StructuredTool
from mcp.types import CallToolResult, TextContent

from app.config import config
from app.tools.base import (
    AIOpsTool,
    ToolExecutionResult,
    ToolRetryPolicy,
    invoke_langchain_tool,
    normalize_langchain_tool_output,
    tool_map,
)
from app.tools.registry import ToolRegistry, create_default_tool_registry


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


class MCPContentBlockTool:
    def __init__(self, name: str, output: dict):
        self.name = name
        self.description = f"MCP content block {name}"
        self.output = output

    async def ainvoke(self, input_args: dict):
        return [{"type": "text", "text": json.dumps(self.output)}]


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


class RestartServiceTool(AIOpsTool):
    name = "restart_service"
    description = "restart service"
    input_schema = {"type": "object"}
    risk_level = "medium"
    read_only = False

    async def _call(self, input_args: dict):
        return {"status": "success", "summary": "restarted"}


class TransientReadOnlyTool(AIOpsTool):
    name = "transient_read"
    read_only = True
    timeout_seconds = 1.0
    retry_policy = ToolRetryPolicy(
        max_attempts=3,
        backoff_seconds=0,
        retry_on=["timeout"],
    )

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def _call(self, input_args: dict):
        self.calls += 1
        if self.calls < 3:
            return {
                "status": "failed",
                "error_type": "timeout",
                "retryable": True,
                "summary": "temporary timeout",
            }
        return {"status": "success", "summary": "recovered"}


class PermissionDeniedTool(AIOpsTool):
    name = "permission_denied_read"
    read_only = True
    retry_policy = ToolRetryPolicy(
        max_attempts=3,
        backoff_seconds=0,
        retry_on=["timeout", "connection_error"],
    )

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def _call(self, input_args: dict):
        self.calls += 1
        return {
            "status": "failed",
            "error_type": "permission_denied",
            "retryable": False,
            "summary": "permission denied",
        }


class RetryingWriteTool(AIOpsTool):
    name = "retrying_write"
    read_only = False
    retry_policy = ToolRetryPolicy(
        max_attempts=3,
        backoff_seconds=0,
        retry_on=["timeout"],
    )

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def _call(self, input_args: dict):
        self.calls += 1
        return {
            "status": "failed",
            "error_type": "timeout",
            "retryable": True,
            "summary": "write timed out",
        }


class TotalBudgetTimeoutTool(AIOpsTool):
    name = "total_budget_timeout"
    read_only = True
    timeout_seconds = 0.03
    retry_policy = ToolRetryPolicy(
        max_attempts=3,
        backoff_seconds=0,
        retry_on=["timeout"],
    )

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def _call(self, input_args: dict):
        self.calls += 1
        await asyncio.sleep(0.1)
        return {"status": "success"}


class RequiredInputTool(AIOpsTool):
    name = "required_input"
    input_schema = {
        "type": "object",
        "properties": {"service_name": {"type": "string", "minLength": 1}},
        "required": ["service_name"],
    }

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def _call(self, input_args: dict):
        self.calls += 1
        return {"status": "success"}


class InvalidSchemaTool(AIOpsTool):
    name = "invalid_schema"
    input_schema = {"type": "definitely-not-a-json-schema-type"}

    async def _call(self, input_args: dict):
        return {"status": "success"}


class DefaultsInputTool(AIOpsTool):
    name = "defaults_input"
    input_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string"},
            "time_range": {"type": "string", "default": "10m"},
            "options": {"type": "object", "default": {"limit": 5}},
        },
        "required": ["service_name"],
    }

    async def _call(self, input_args: dict):
        input_args["options"]["limit"] = 6
        return {"status": "success"}


class CancelledTool(AIOpsTool):
    name = "cancelled_tool"

    async def _call(self, input_args: dict):
        raise asyncio.CancelledError


class InvalidOutputTool(AIOpsTool):
    name = "invalid_output"
    output_schema = {
        "type": "object",
        "properties": {"signals": {"type": "object"}},
        "required": ["signals"],
    }

    async def _call(self, input_args: dict):
        return {"summary": "missing required signals"}


class MisdeclaredReadOnlyTool(AIOpsTool):
    name = "query_misdeclared_action"
    read_only = True
    risk_level = "low"

    async def _call(self, input_args: dict):
        return {"status": "success"}


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


def test_registry_rejects_duplicate_tool_names() -> None:
    registry = ToolRegistry()
    registry.register(MutableContractTool())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(MutableContractTool())


def test_tool_execution_result_normalizes_structured_failure_semantics() -> None:
    result = ToolExecutionResult(
        tool_name="query_metrics",
        status="success",
        output={
            "status": "failed",
            "error_type": "server_error",
            "error_message": "backend unavailable",
        },
    )

    assert result.status == "failed"
    assert result.error_message == "backend unavailable"


def test_discovered_tool_map_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="Duplicate discovered tool name"):
        tool_map(
            [
                FakeAsyncTool("query_cpu_metrics", {}),
                FakeAsyncTool("query_cpu_metrics", {}),
            ]
        )


def test_registry_test_fixture_replacement_requires_existing_name() -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="not registered"):
        registry.replace_for_test(MutableContractTool())

    original = MutableContractTool()
    replacement = MutableContractTool()
    registry.register(original)
    registry.replace_for_test(replacement)

    assert registry.get(replacement.name) is replacement


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
    assert redis_contract["retry_policy"]["max_attempts"] == 2
    assert redis_contract["retry_policy"]["retry_on"] == [
        "timeout",
        "connection_error",
        "server_error",
    ]

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
async def test_read_only_tool_retries_transient_failure_and_records_attempts() -> None:
    tool = TransientReadOnlyTool()

    result = await tool.arun({"service_name": "order-service"})

    assert result.status == "success"
    assert tool.calls == 3
    retry = result.metadata["retry"]
    assert retry["attempt_count"] == 3
    assert retry["retried"] is True
    assert retry["retry_exhausted"] is False
    assert retry["stop_reason"] == "success"
    assert [item["failure_kind"] for item in retry["attempts"][:2]] == [
        "timeout",
        "timeout",
    ]


@pytest.mark.asyncio
async def test_tool_does_not_retry_non_retryable_failure() -> None:
    tool = PermissionDeniedTool()

    result = await tool.arun({})

    assert result.status == "failed"
    assert tool.calls == 1
    assert result.metadata["retry"]["attempt_count"] == 1
    assert result.metadata["retry"]["stop_reason"] == "non_retryable_failure"


@pytest.mark.asyncio
async def test_non_read_only_tool_never_retries_automatically() -> None:
    tool = RetryingWriteTool()

    result = await tool.arun({})

    assert result.status == "failed"
    assert tool.calls == 1
    assert result.metadata["retry"]["max_attempts"] == 1
    assert result.metadata["retry"]["retried"] is False


@pytest.mark.asyncio
async def test_tool_timeout_uses_total_budget_without_starting_phantom_retry() -> None:
    tool = TotalBudgetTimeoutTool()

    result = await tool.arun({})

    assert result.status == "failed"
    assert tool.calls == 1
    retry = result.metadata["retry"]
    assert retry["attempt_count"] == 1
    assert retry["attempts"][0]["failure_kind"] == "timeout"
    assert retry["stop_reason"] == "total_timeout_exhausted"
    assert retry["retry_exhausted"] is False


@pytest.mark.asyncio
async def test_registry_enforces_input_contract_on_real_plan_execution_path() -> None:
    registry = ToolRegistry()
    tool = RequiredInputTool()
    registry.register(tool, trusted=True)

    result = await registry.arun(
        "required_input",
        {},
        step={
            "tool_name": "required_input",
            "purpose": "validate required input",
            "input_args": {},
        },
    )

    assert result.status == "failed"
    assert result.output["error_type"] == "invalid_input"
    assert tool.calls == 0


def test_registry_rejects_invalid_tool_schema_during_registration() -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="invalid input schema"):
        registry.register(InvalidSchemaTool(), trusted=True)


@pytest.mark.asyncio
async def test_default_registry_rejects_unknown_arguments_before_adapter_call() -> None:
    registry = create_default_tool_registry([])

    result = await registry.arun(
        "query_k8s_status",
        {"service_name": "order-service", "command": "delete pod"},
    )

    assert result.status == "failed"
    assert result.output["error_type"] == "invalid_input"
    assert result.output["validation_error"]["validator"] == "additionalProperties"


@pytest.mark.asyncio
async def test_registry_applies_schema_defaults_before_policy_and_execution() -> None:
    registry = ToolRegistry()
    registry.register(DefaultsInputTool(), trusted=True)

    result = await registry.arun("defaults_input", {"service_name": "order-service"})

    assert result.status == "success"
    assert result.input_args["time_range"] == "10m"
    assert result.input_args["options"] == {"limit": 6}
    assert DefaultsInputTool.input_schema["properties"]["options"]["default"] == {"limit": 5}


@pytest.mark.asyncio
async def test_registry_rejects_success_that_violates_output_contract() -> None:
    registry = ToolRegistry()
    registry.register(InvalidOutputTool(), trusted=True)

    result = await registry.arun("invalid_output", {})

    assert result.status == "failed"
    assert result.output["error_type"] == "invalid_output"
    assert result.output["validation_error"]["validator"] == "required"


@pytest.mark.asyncio
async def test_untrusted_tool_cannot_self_declare_as_low_risk_read_only() -> None:
    registry = ToolRegistry()
    tool = MisdeclaredReadOnlyTool()
    registry.register(tool)

    result = await registry.arun("query_misdeclared_action", {})

    assert result.status == "failed"
    assert result.output["policy"] == "approval_required"
    assert result.risk_level == "high"
    assert result.read_only is False
    assert "tool:not-read-only" in result.output["matched_rules"]


@pytest.mark.asyncio
async def test_aiops_tool_propagates_cancellation() -> None:
    with pytest.raises(asyncio.CancelledError):
        await CancelledTool().arun({})


def test_normalize_call_tool_result_error_envelope() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="backend rejected request")],
        isError=True,
    )

    normalized = normalize_langchain_tool_output(result)

    assert normalized == {
        "status": "failed",
        "error_type": "mcp_error",
        "error_message": "backend rejected request",
    }


@pytest.mark.asyncio
async def test_invoke_langchain_tool_normalizes_mcp_text_content_object() -> None:
    class TextContentTool:
        name = "text_content_tool"

        async def ainvoke(self, input_args: dict):
            return TextContent(
                type="text",
                text=json.dumps(
                    {
                        "status": "failed",
                        "error_type": "server_error",
                        "error_message": "backend unavailable",
                    }
                ),
            )

    normalized = await invoke_langchain_tool(TextContentTool(), {})

    assert normalized["status"] == "failed"
    assert normalized["error_type"] == "server_error"


@pytest.mark.asyncio
async def test_invoke_langchain_tool_rejects_unknown_schema_arguments() -> None:
    async def coroutine(service_name: str) -> dict:
        return {"service_name": service_name}

    tool = StructuredTool.from_function(
        name="schema_tool",
        description="schema tool",
        coroutine=coroutine,
    )

    with pytest.raises(ValueError, match="unsupported arguments: time_range"):
        await invoke_langchain_tool(
            tool,
            {"service_name": "order-service", "time_range": "10m"},
        )


@pytest.mark.asyncio
async def test_invoke_langchain_tool_rejects_unknown_json_schema_arguments() -> None:
    class JsonSchemaTool:
        name = "json_schema_tool"
        args_schema = {
            "type": "object",
            "properties": {"service_name": {"type": "string"}},
        }

        async def ainvoke(self, input_args: dict):
            return input_args

    with pytest.raises(ValueError, match="unsupported arguments: time_range"):
        await invoke_langchain_tool(
            JsonSchemaTool(),
            {"service_name": "order-service", "time_range": "10m"},
        )


@pytest.mark.asyncio
async def test_registry_policy_guard_blocks_non_read_only_prod_action() -> None:
    registry = ToolRegistry().with_incident_context(
        {"environment": "prod", "service_name": "order-service"}
    )
    registry.register(RestartServiceTool())

    result = await registry.arun(
        "restart_service",
        {"service_name": "order-service"},
    )

    assert result.status == "failed"
    assert result.output["source"] == "policy_guard"
    assert result.output["policy"] == "approval_required"
    assert result.risk_level == "high"
    assert result.read_only is False
    assert "tool:not-read-only" in result.output["matched_rules"]


@pytest.mark.asyncio
async def test_registry_rejects_policy_step_for_a_different_tool() -> None:
    registry = ToolRegistry().with_incident_context(
        {"environment": "prod", "service_name": "order-service"}
    )
    tool = RestartServiceTool()
    registry.register(tool)

    result = await registry.arun(
        "restart_service",
        {"service_name": "order-service"},
        step={
            "tool_name": "query_metrics",
            "purpose": "query metrics",
            "input_args": {"service_name": "order-service"},
        },
    )

    assert result.status == "failed"
    assert result.output["policy"] == "forbidden"
    assert result.output["matched_rules"] == ["tool:step-name-mismatch"]
    assert result.risk_level == "high"


@pytest.mark.asyncio
async def test_registry_rejects_arguments_that_differ_from_policy_step() -> None:
    registry = create_default_tool_registry([])

    result = await registry.arun(
        "query_metrics",
        {"service_name": "payment-service"},
        step={
            "tool_name": "query_metrics",
            "purpose": "query approved service metrics",
            "input_args": {"service_name": "order-service"},
        },
    )

    assert result.status == "failed"
    assert result.output["policy"] == "forbidden"
    assert result.output["matched_rules"] == ["tool:step-input-mismatch"]
    assert result.risk_level == "high"


@pytest.mark.asyncio
async def test_registry_fails_closed_for_non_json_serializable_success_output() -> None:
    class NonSerializableTool(AIOpsTool):
        name = "non_serializable"
        input_schema = {"type": "object"}
        output_schema = {"type": "object"}

        async def _call(self, input_args: dict[str, object]) -> dict[str, object]:
            return {"value": object()}

    registry = ToolRegistry()
    registry.register(NonSerializableTool(), trusted=True)

    result = await registry.arun("non_serializable", {})

    assert result.status == "failed"
    assert result.output["error_type"] == "invalid_output"
    assert result.output["validation_error"]["validator"] == "json_serializable"


@pytest.mark.asyncio
async def test_registry_fails_closed_for_oversized_success_output(monkeypatch) -> None:
    class OversizedTool(AIOpsTool):
        name = "oversized"
        input_schema = {"type": "object"}
        output_schema = {"type": "object"}

        async def _call(self, input_args: dict[str, object]) -> dict[str, object]:
            return {"value": "x" * 128}

    monkeypatch.setattr("app.tools.registry.MAX_TOOL_OUTPUT_BYTES", 64)
    registry = ToolRegistry()
    registry.register(OversizedTool(), trusted=True)

    result = await registry.arun("oversized", {})

    assert result.status == "failed"
    assert result.output["validation_error"]["validator"] == "max_output_bytes"


@pytest.mark.asyncio
async def test_query_metrics_treats_real_mcp_content_block_failures_as_failed(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "prometheus_base_url", "")
    registry = create_default_tool_registry(
        [
            MCPContentBlockTool(
                "query_cpu_metrics",
                {
                    "status": "failed",
                    "error_type": "server_error",
                    "error_message": "CPU backend unavailable",
                },
            ),
            MCPContentBlockTool(
                "query_memory_metrics",
                {
                    "status": "failed",
                    "error_type": "server_error",
                    "error_message": "memory backend unavailable",
                },
            ),
        ]
    )

    result = await registry.arun("query_metrics", {"service_name": "order-service"})

    assert result.status == "failed"
    assert result.output["source"] == "mcp_monitor_mixed"
    assert "partially available" in result.output["summary"]
    assert len(result.output["partial_errors"]) == 2
    assert result.metadata["retry"]["attempt_count"] == 1
    assert result.metadata["retry"]["stop_reason"] == "non_retryable_failure"


@pytest.mark.asyncio
async def test_query_metrics_normalizes_langchain_structured_tool_content_blocks(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "prometheus_base_url", "")

    async def cpu_coroutine(**_: object):
        return (
            [{"type": "text", "text": json.dumps({"status": "failed", "error": "boom"})}],
            None,
        )

    async def memory_coroutine(**_: object):
        return (
            [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "status": "success",
                            "metric_name": "memory_usage_percent",
                            "statistics": {"max": 76},
                        }
                    ),
                }
            ],
            None,
        )

    schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string"},
            "interval": {"type": "string"},
        },
        "required": ["service_name"],
    }
    registry = create_default_tool_registry(
        [
            StructuredTool(
                name="query_cpu_metrics",
                description="CPU",
                args_schema=schema,
                coroutine=cpu_coroutine,
                response_format="content_and_artifact",
            ),
            StructuredTool(
                name="query_memory_metrics",
                description="memory",
                args_schema=schema,
                coroutine=memory_coroutine,
                response_format="content_and_artifact",
            ),
        ]
    )

    result = await registry.arun("query_metrics", {"service_name": "order-service"})

    assert result.status == "failed"
    assert result.output["source"] == "mcp_monitor_mixed"
    assert result.output["available_metrics"]["memory"]["metric_name"] == ("memory_usage_percent")
    assert result.output["partial_errors"][0]["error_message"] == "外部依赖暂时不可用"
    assert "boom" not in str(result.output)


@pytest.mark.asyncio
async def test_query_metrics_marks_synthetic_mcp_monitor_as_mock() -> None:
    registry = create_default_tool_registry(
        [
            FakeAsyncTool(
                "query_cpu_metrics",
                {
                    "status": "success",
                    "source": "mock",
                    "synthetic": True,
                    "metric_name": "cpu_usage_percent",
                },
            ),
            FakeAsyncTool(
                "query_memory_metrics",
                {
                    "status": "success",
                    "source": "mock",
                    "synthetic": True,
                    "metric_name": "memory_usage_percent",
                },
            ),
        ]
    )

    result = await registry.arun("query_metrics", {"service_name": "order-service"})

    assert result.status == "success"
    assert result.output["source"] == "mock"
    assert result.output["source_detail"] == {
        "cpu": "mock",
        "memory": "mock",
        "qps": "unavailable",
        "p95_latency_ms": "unavailable",
        "error_rate": "unavailable",
    }
    assert result.output["synthetic_fields"] == ["cpu", "memory"]


@pytest.mark.asyncio
async def test_query_metrics_fails_partial_mcp_without_synthetic_backfill(monkeypatch) -> None:
    monkeypatch.setattr(config, "prometheus_base_url", "")
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
    assert "partially available" in result.output["summary"]
    assert result.output["available_metrics"]["memory"]["metric_name"] == ("memory_usage_percent")


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
