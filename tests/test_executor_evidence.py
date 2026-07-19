"""Tests for Executor evidence and tool-call audit records."""

import asyncio
import importlib
import json
from typing import Any

import pytest

from app.agent.aiops import create_initial_aiops_state
from app.agent.aiops.execution_fallbacks import FallbackExecutionOutcome
from app.config import config
from app.models.plan import PlanStep
from app.tools.base import AIOpsTool, ToolExecutionResult
from app.tools.registry import ToolRegistry
from app.utils.public_errors import GENERIC_OPERATION_ERROR

executor_module = importlib.import_module("app.agent.aiops.executor")


class EmptyMCPClient:
    async def get_tools(self) -> list[Any]:
        return []


async def fake_get_mcp_client_with_retry() -> EmptyMCPClient:
    return EmptyMCPClient()


class FakeNamedTool:
    def __init__(self, name: str):
        self.name = name


class FanoutCoordinator:
    def __init__(self, target_count: int) -> None:
        self.target_count = target_count
        self.started: list[str] = []
        self.finished: list[str] = []
        self._all_started = asyncio.Event()

    async def wait_until_batch_started(self, tool_name: str) -> None:
        self.started.append(tool_name)
        if len(self.started) >= self.target_count:
            self._all_started.set()
        await asyncio.wait_for(self._all_started.wait(), timeout=0.5)
        self.finished.append(tool_name)


class CoordinatedFanoutTool(AIOpsTool):
    description = "coordinated read-only fanout test tool"
    risk_level = "low"
    read_only = True
    timeout_seconds = 1.0

    def __init__(
        self,
        name: str,
        coordinator: FanoutCoordinator | None = None,
        *,
        status: str = "success",
    ) -> None:
        super().__init__()
        self.name = name
        self._coordinator = coordinator
        self._status = status

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        if self._coordinator:
            await self._coordinator.wait_until_batch_started(self.name)
        if self._status == "failed":
            return {
                "status": "failed",
                "source": "failed",
                "summary": f"{self.name} failed",
                "error_message": f"{self.name} unavailable",
            }
        return {
            "source": "prometheus",
            "summary": f"{self.name} ok",
            "fact": f"{self.name} returned usable evidence",
        }


def create_fanout_registry(
    *,
    coordinator: FanoutCoordinator | None = None,
    failed_tool: str = "",
) -> ToolRegistry:
    registry = ToolRegistry()
    for name in [
        "query_metrics",
        "query_logs",
        "query_redis_status",
        "query_mysql_status",
        "query_k8s_status",
        "search_runbook",
        "suggest_remediation",
    ]:
        registry.register(
            CoordinatedFanoutTool(
                name,
                coordinator if name in {"query_metrics", "query_logs"} else None,
                status="failed" if name == failed_tool else "success",
            ),
            trusted=True,
        )
    return registry


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
        error_message="password=error-secret",
        metadata={"debug": "token=metadata-secret"},
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
    assert persisted.error_message == "password=[REDACTED]"
    assert persisted.metadata["debug"] == "token=[REDACTED]"

    step = PlanStep(
        step_id="s-redact",
        tool_name="query_logs",
        purpose="check logs",
        input_args={"service_name": "order-service"},
    )
    evidence = executor_module._tool_result_to_evidence(persisted, step).model_dump(mode="json")
    assert "error-secret" not in str(evidence)
    assert "metadata-secret" not in str(evidence)


def test_tool_call_record_builder_redacts_raw_results_without_preprocessing() -> None:
    result = ToolExecutionResult(
        tool_name="query_logs",
        status="success",
        input_args={"api_token": "input-secret"},
        output={"summary": "token=output-secret"},
        error_message="password=error-secret",
        metadata={"cookie": "metadata-secret"},
    )
    step = PlanStep(step_id="s-record-redact", tool_name="query_logs", purpose="check logs")

    record = executor_module._tool_result_to_call_record(
        result,
        step,
        {"trace_id": "trace-record-redact", "incident": {"incident_id": "inc-record-redact"}},
    )
    serialized = record.model_dump_json()

    for secret in ("input-secret", "output-secret", "error-secret", "metadata-secret"):
        assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_evidence_builder_redacts_raw_results_without_preprocessing() -> None:
    result = ToolExecutionResult(
        tool_name="query_logs",
        status="success",
        input_args={"api_token": "input-secret"},
        output={"summary": "Authorization: Bearer output-secret"},
        metadata={"password": "metadata-secret"},
    )
    step = PlanStep(step_id="s-evidence-redact", tool_name="query_logs", purpose="check logs")

    evidence = executor_module._tool_result_to_evidence(result, step)
    serialized = evidence.model_dump_json()

    for secret in ("input-secret", "output-secret", "metadata-secret"):
        assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_executor_persistence_compacts_top_level_processlist_sample() -> None:
    result = ToolExecutionResult(
        tool_name="query_mysql_status",
        status="success",
        output={
            "source": "mysql",
            "summary": "mysql ok",
            "processlist_sample": [
                {
                    "Id": 12,
                    "User": "internal_user",
                    "Host": "10.0.0.8:3306",
                    "db": "payments",
                    "Command": "Query",
                    "Time": 5,
                    "State": "executing",
                    "Info": "SELECT card_number FROM payment_secrets",
                }
            ],
        },
    )

    persisted = executor_module._result_for_persistence(result)

    assert persisted.output["processlist_sample"] == [
        {
            "Command": "Query",
            "Time": 5,
            "State": "executing",
            "has_statement": True,
        }
    ]
    assert "internal_user" not in str(persisted.output)
    assert "payment_secrets" not in str(persisted.output)


def test_executor_persistence_removes_internal_endpoint_and_publicizes_fallback_errors() -> None:
    result = ToolExecutionResult(
        tool_name="query_logs",
        status="success",
        output={
            "source": "loki",
            "endpoint": "loki.internal:3100",
            "summary": "fallback succeeded",
            "fallback_errors": [
                {
                    "source": "log_gateway",
                    "error_type": "connection_error",
                    "error_message": "http://logs.internal:8080 unavailable",
                }
            ],
        },
    )

    persisted = executor_module._result_for_persistence(result)

    assert "endpoint" not in persisted.output
    assert "logs.internal" not in str(persisted.output)
    assert persisted.output["fallback_errors"][0]["error_type"] == "connection_error"


def test_executor_persistence_normalizes_success_with_empty_output_to_failure() -> None:
    result = ToolExecutionResult(
        tool_name="query_metrics",
        status="success",
        input_args={"service_name": "order-service"},
        output=None,
    )
    step = PlanStep(
        step_id="s-empty",
        tool_name="query_metrics",
        purpose="check metrics",
    )

    persisted = executor_module._result_for_persistence(result)
    evidence = executor_module._tool_result_to_evidence(persisted, step)

    assert persisted.status == "failed"
    assert persisted.error_message == "Tool returned no usable data"
    assert persisted.metadata["evidence_quality"]["status"] == "failed"
    assert evidence.stance == "unknown"
    assert evidence.confidence == 0.1


def test_executor_stale_result_is_not_supporting_evidence() -> None:
    result = ToolExecutionResult(
        tool_name="query_redis_status",
        status="success",
        output={
            "source": "redis_info",
            "stale": True,
            "connected_clients": 10000,
            "maxclients": 10000,
            "summary": "cached Redis saturation result",
        },
    )
    step = PlanStep(
        step_id="s-stale",
        tool_name="query_redis_status",
        purpose="check Redis",
    )

    persisted = executor_module._result_for_persistence(result)
    evidence = executor_module._tool_result_to_evidence(persisted, step)

    assert persisted.status == "success"
    assert persisted.metadata["evidence_quality"]["status"] == "stale"
    assert evidence.stance == "unknown"
    assert evidence.confidence == 0.15
    assert "过期" in evidence.uncertainty


def test_executor_persistence_materializes_large_tool_output_artifact(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(config, "aiops_tool_output_inline_bytes", 120)
    monkeypatch.setattr(config, "aiops_tool_output_artifact_dir", str(tmp_path / "artifacts"))
    result = ToolExecutionResult(
        tool_name="query_logs",
        status="success",
        input_args={"service_name": "order-service", "api_token": "secret-token"},
        output={
            "source": "loki",
            "summary": "large log payload",
            "lines": [f"line-{index} token=line-secret" for index in range(40)],
        },
    )

    persisted = executor_module._result_for_persistence(result)
    artifact = persisted.metadata["output_artifact"]
    artifact_path = tmp_path / "artifacts" / f"{artifact['artifact_id']}.json"
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    step = PlanStep(
        step_id="s-artifact",
        tool_name="query_logs",
        purpose="检查日志",
        input_args={"service_name": "order-service"},
        expected_evidence="日志证据",
    )
    evidence = executor_module._tool_result_to_evidence(persisted, step).model_dump(mode="json")
    record = executor_module._tool_result_to_call_record(
        persisted,
        step,
        {"trace_id": "trace-artifact", "incident": {"incident_id": "inc-artifact"}},
    ).model_dump(mode="json")

    assert persisted.output["truncated"] is True
    assert persisted.output["artifact_id"] == artifact["artifact_id"]
    assert persisted.output["source"] == "loki"
    assert artifact["size_bytes"] > 120
    assert artifact_path.exists()
    assert payload["input_args"]["api_token"] == "[REDACTED]"
    assert "line-secret" not in artifact_path.read_text(encoding="utf-8")
    assert evidence["artifact_refs"][0]["artifact_id"] == artifact["artifact_id"]
    assert record["output_artifact"]["artifact_id"] == artifact["artifact_id"]


def state_with_step(step: PlanStep) -> dict[str, Any]:
    state = create_initial_aiops_state(
        "diagnose order-service Redis timeout",
        session_id="executor-evidence-test",
    )
    state["current_plan"] = [step.model_dump(mode="json")]
    state["plan"] = [step.purpose]
    return state


def state_with_steps(steps: list[PlanStep]) -> dict[str, Any]:
    state = create_initial_aiops_state(
        "diagnose order-service Redis timeout",
        session_id="executor-fanout-test",
    )
    state["current_plan"] = [step.model_dump(mode="json") for step in steps]
    state["plan"] = [executor_module._format_plan_step_for_execution(step) for step in steps]
    return state


@pytest.mark.asyncio
async def test_executor_fails_closed_for_legacy_text_only_plan(monkeypatch) -> None:
    monkeypatch.setattr(
        executor_module.trace_service,
        "record_risk_decision",
        lambda **_: None,
    )
    state = create_initial_aiops_state(
        "restart production service",
        session_id="executor-legacy-plan-risk",
    )
    state["plan"] = ["restart production service"]

    update = await executor_module.executor(state)

    assert update["risk_assessment"]["policy"] == "forbidden"
    assert update["risk_assessment"]["risk_level"] == "high"
    assert "plan:legacy-unassessed" in update["risk_assessment"]["matched_rules"]
    assert update["pending_approval"] is None
    assert update["errors"]
    assert update["current_plan"] == []
    assert update["plan"] == ["restart production service"]


@pytest.mark.asyncio
async def test_executor_fails_closed_for_invalid_structured_step_with_legacy_plan(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        executor_module.trace_service,
        "record_risk_decision",
        lambda **_: None,
    )
    state = create_initial_aiops_state(
        "diagnose invalid plan",
        session_id="executor-invalid-plan-risk",
    )
    state["current_plan"] = [
        {
            "step_id": "broken",
            "tool_name": "restart_service",
            "purpose": "restart production service",
            "input_args": [],
        }
    ]
    state["plan"] = ["restart production service"]

    update = await executor_module.executor(state)

    assert update["risk_assessment"]["policy"] == "forbidden"
    assert "plan:invalid-step" in update["risk_assessment"]["matched_rules"]
    assert update["pending_approval"] is None
    assert update["current_plan"] == state["current_plan"]
    assert update["plan"] == state["plan"]


@pytest.mark.asyncio
async def test_executor_runs_adjacent_read_only_evidence_steps_as_ordered_fanout(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        executor_module,
        "get_mcp_client_with_retry",
        fake_get_mcp_client_with_retry,
    )
    coordinator = FanoutCoordinator(target_count=2)
    monkeypatch.setattr(
        executor_module,
        "create_default_tool_registry",
        lambda _: create_fanout_registry(coordinator=coordinator),
    )
    steps = [
        PlanStep(
            step_id="s1",
            tool_name="query_metrics",
            purpose="检查服务指标",
            input_args={"service_name": "order-service"},
            expected_evidence="指标侧影响证据",
        ),
        PlanStep(
            step_id="s2",
            tool_name="query_logs",
            purpose="检查应用日志",
            input_args={"service_name": "order-service", "query": "ERROR OR timeout"},
            expected_evidence="日志侧错误证据",
        ),
        PlanStep(
            step_id="s3",
            tool_name="suggest_remediation",
            purpose="生成修复建议，不执行生产变更",
            input_args={"service_name": "order-service"},
            expected_evidence="风险受控的处置建议",
            risk_level="medium",
        ),
    ]

    update = await executor_module.executor(state_with_steps(steps))

    assert set(coordinator.started) == {"query_metrics", "query_logs"}
    assert update["current_plan"][0]["tool_name"] == "suggest_remediation"
    assert [record["tool_name"] for record in update["tool_call_records"]] == [
        "query_metrics",
        "query_logs",
    ]
    assert [step["status"] for step in update["executed_steps"]] == ["success", "success"]
    assert [item["source_tool"] for item in update["gathered_evidence"]] == [
        "query_metrics",
        "query_logs",
    ]
    assert [
        item["raw_data"]["metadata"]["evidence_batch"]["batch_index"]
        for item in update["gathered_evidence"]
    ] == [1, 2]
    assert all(
        item["raw_data"]["metadata"]["evidence_batch"]["execution_mode"]
        == "bounded_read_only_fanout"
        for item in update["gathered_evidence"]
    )


@pytest.mark.asyncio
async def test_executor_fanout_tool_failure_becomes_failed_evidence_without_breaking_batch(
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
        lambda _: create_fanout_registry(failed_tool="query_logs"),
    )
    steps = [
        PlanStep(
            step_id="s1",
            tool_name="query_metrics",
            purpose="检查服务指标",
            input_args={"service_name": "order-service"},
            expected_evidence="指标证据",
        ),
        PlanStep(
            step_id="s2",
            tool_name="query_logs",
            purpose="检查日志",
            input_args={"service_name": "order-service", "query": "ERROR"},
            expected_evidence="日志证据",
        ),
        PlanStep(
            step_id="s3",
            tool_name="query_redis_status",
            purpose="检查 Redis",
            input_args={"service_name": "order-service"},
            expected_evidence="Redis 证据",
        ),
    ]

    update = await executor_module.executor(state_with_steps(steps))

    assert update["current_plan"] == []
    assert [record["status"] for record in update["tool_call_records"]] == [
        "success",
        "failed",
        "success",
    ]
    assert update["gathered_evidence"][1]["raw_data"]["status"] == "failed"
    assert "工具 query_logs 步骤 s2 调用失败" in update["errors"][0]


@pytest.mark.asyncio
async def test_executor_fanout_stops_before_policy_blocked_read_only_input(
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
        lambda _: create_fanout_registry(),
    )
    steps = [
        PlanStep(
            step_id="s1",
            tool_name="query_metrics",
            purpose="检查服务指标",
            input_args={"service_name": "order-service"},
            expected_evidence="指标证据",
        ),
        PlanStep(
            step_id="s2",
            tool_name="query_logs",
            purpose="检索危险命令文本，应该由 policy guard 单步处理",
            input_args={"service_name": "order-service", "query": "kubectl delete pod"},
            expected_evidence="日志证据",
        ),
        PlanStep(
            step_id="s3",
            tool_name="query_redis_status",
            purpose="检查 Redis",
            input_args={"service_name": "order-service"},
            expected_evidence="Redis 证据",
        ),
    ]

    update = await executor_module.executor(state_with_steps(steps))

    assert [record["tool_name"] for record in update["tool_call_records"]] == ["query_metrics"]
    assert [step["tool_name"] for step in update["current_plan"]] == [
        "query_logs",
        "query_redis_status",
    ]
    assert update["executed_steps"][0]["step_id"] == "s1"


@pytest.mark.asyncio
async def test_executor_fanout_is_bounded_to_four_read_only_evidence_steps(
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
        lambda _: create_fanout_registry(),
    )
    steps = [
        PlanStep(
            step_id=f"s{index}",
            tool_name=tool_name,
            purpose=f"执行 {tool_name}",
            input_args={"service_name": "order-service"},
            expected_evidence=f"{tool_name} 证据",
        )
        for index, tool_name in enumerate(
            [
                "query_metrics",
                "query_logs",
                "query_redis_status",
                "query_mysql_status",
                "query_k8s_status",
            ],
            1,
        )
    ]

    update = await executor_module.executor(state_with_steps(steps))

    assert [record["tool_name"] for record in update["tool_call_records"]] == [
        "query_metrics",
        "query_logs",
        "query_redis_status",
        "query_mysql_status",
    ]
    assert [step["tool_name"] for step in update["current_plan"]] == ["query_k8s_status"]


@pytest.mark.asyncio
async def test_executor_registry_step_records_unconfigured_adapter_without_mock(
    monkeypatch,
) -> None:
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
    assert update["executed_steps"][0]["status"] == "failed"
    assert update["plan"] == []

    evidence = update["gathered_evidence"][0]
    assert evidence["source_tool"] == "query_redis_status"
    assert evidence["step_id"] == "s1"
    assert evidence["evidence_type"] == "redis"
    assert evidence["data_source"] == "not_configured"
    assert evidence["stance"] == "unknown"
    assert "工具失败" in evidence["confidence_reason"]
    assert evidence["confidence"] == 0.05
    assert "调用失败" in evidence["summary"]
    assert "来源=not_configured" in evidence["fact"]
    assert "证据缺口" in evidence["inference"]
    assert "真实适配器未配置" in evidence["uncertainty"]
    assert "配置 query_redis_status 对应真实适配器" in evidence["next_step"]
    assert evidence["raw_data"]["status"] == "failed"
    assert evidence["raw_data"]["output"]["error_type"] == "not_configured"

    record = update["tool_call_records"][0]
    assert record["trace_id"] == state["trace_id"]
    assert record["incident_id"] == state["incident"]["incident_id"]
    assert record["step_id"] == "s1"
    assert record["tool_name"] == "query_redis_status"
    assert record["input_args"]["service_name"] == "order-service"
    assert "order-service" in record["input_summary"]
    assert record["data_source"] == "not_configured"
    assert "调用失败" in record["output_summary"]
    assert record["risk_level"] == "low"
    assert record["read_only"] is True
    assert record["status"] == "failed"
    assert record["error_message"] == "外部依赖未配置"
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
    registry.register(FailingRedisTool(), trusted=True)
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
    assert evidence["raw_data"]["error_message"] == GENERIC_OPERATION_ERROR
    assert "redis backend unavailable" not in str(evidence["raw_data"])

    record = update["tool_call_records"][0]
    assert record["status"] == "failed"
    assert record["error_message"] == GENERIC_OPERATION_ERROR
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
    registry.register(StructuredFailingRedisTool(), trusted=True)
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
    assert update["tool_call_records"][0]["invocation_kind"] == "analysis_fallback"
    assert update["tool_call_records"][0]["actual_tool_invoked"] is False


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


@pytest.mark.asyncio
async def test_executor_fallback_records_actual_safe_tool_call(monkeypatch) -> None:
    monkeypatch.setattr(
        executor_module,
        "get_mcp_client_with_retry",
        fake_get_mcp_client_with_retry,
    )

    async def fake_llm_executor(task: str, all_tools: list[Any]) -> FallbackExecutionOutcome:
        return FallbackExecutionOutcome(
            text="fallback used current time",
            tool_results=[
                ToolExecutionResult(
                    tool_name="get_current_time",
                    status="success",
                    input_args={},
                    output={"summary": "2026-07-18 12:00:00"},
                    latency_ms=4.5,
                    metadata={
                        "execution_path": "llm_toolnode_fallback",
                        "invocation_kind": "tool",
                        "actual_tool_invoked": True,
                    },
                )
            ],
        )

    monkeypatch.setattr(executor_module, "_execute_with_llm_tools", fake_llm_executor)
    step = PlanStep(
        step_id="s-fallback-audit",
        tool_name="query_unregistered_system",
        purpose="检查未注册系统",
        input_args={"service_name": "order-service"},
    )

    update = await executor_module.executor(state_with_step(step))

    assert [item["tool_name"] for item in update["tool_call_records"]] == [
        "get_current_time",
        "query_unregistered_system",
    ]
    actual = update["tool_call_records"][0]
    assert actual["status"] == "success"
    assert actual["actual_tool_invoked"] is True
    assert actual["invocation_kind"] == "tool"
    wrapper = update["tool_call_records"][1]
    assert wrapper["status"] == "failed"
    assert wrapper["actual_tool_invoked"] is False


@pytest.mark.asyncio
async def test_fanout_cancellation_records_partial_and_cancelled_calls(monkeypatch) -> None:
    completed = asyncio.Event()
    release_slow = asyncio.Event()
    recorded: list[dict[str, Any]] = []

    async def fake_fanout_item(
        step: PlanStep,
        registry: ToolRegistry,
        state: dict[str, Any],
        *,
        batch_metadata: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
        if step.step_id == "s-fast":
            completed.set()
            return (
                "ok",
                "success",
                {},
                {
                    "trace_id": state["trace_id"],
                    "incident_id": state["incident"]["incident_id"],
                    "step_id": step.step_id,
                    "tool_name": step.tool_name,
                    "status": "success",
                    "execution_metadata": {"evidence_batch": dict(batch_metadata)},
                },
            )
        await release_slow.wait()
        raise AssertionError("slow task should be cancelled")

    class TraceCollector:
        def record_tool_call(self, record: Any) -> None:
            recorded.append(
                record.model_dump(mode="json") if hasattr(record, "model_dump") else dict(record)
            )

    monkeypatch.setattr(executor_module, "_execute_registered_fanout_item", fake_fanout_item)
    monkeypatch.setattr(executor_module, "trace_service", TraceCollector())
    state = create_initial_aiops_state(
        "cancel fanout",
        session_id="fanout-cancel",
    )
    steps = [
        PlanStep(step_id="s-fast", tool_name="query_metrics", purpose="fast"),
        PlanStep(step_id="s-slow", tool_name="query_logs", purpose="slow"),
    ]
    task = asyncio.create_task(
        executor_module._execute_registered_step_fanout(steps, ToolRegistry(), state)
    )
    await asyncio.wait_for(completed.wait(), timeout=0.5)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert [item["step_id"] for item in recorded] == ["s-fast", "s-slow"]
    assert recorded[0]["status"] == "success"
    assert (
        recorded[0]["execution_metadata"]["evidence_batch"]["commit_status"]
        == "cancelled_before_state_commit"
    )
    assert recorded[1]["status"] == "failed"
    assert recorded[1]["execution_metadata"]["failure_kind"] == "cancelled"
