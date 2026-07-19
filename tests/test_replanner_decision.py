"""Tests for Replanner decisions based on structured evidence."""

import importlib
from typing import Any

import pytest

from app.agent.aiops import create_initial_aiops_state
from app.agent.aiops.evidence_analyzer import EvidenceAnalysis
from app.models.evidence import (
    Evidence,
    build_confidence_reason,
    infer_evidence_stance,
    infer_evidence_type,
)
from app.models.plan import PlanStep
from app.tools.base import ToolExecutionResult

replanner_module = importlib.import_module("app.agent.aiops.replanner")


def evidence_from_tool(tool_name: str, output: dict, step_id: str) -> dict:
    result = ToolExecutionResult(
        tool_name=tool_name,
        status="success",
        input_args={"service_name": "order-service"},
        output=output,
    )
    raw_data = result.model_dump(mode="json")
    stance = infer_evidence_stance(
        source_tool=tool_name,
        raw_data=raw_data,
        summary=str(output.get("summary", "")),
    )
    return Evidence(
        source_tool=tool_name,
        step_id=step_id,
        summary=str(output.get("summary", "")),
        evidence_type=infer_evidence_type(tool_name),
        stance=stance,
        confidence_reason=build_confidence_reason(
            source_tool=tool_name,
            raw_data=raw_data,
            stance=stance,
        ),
        raw_data=raw_data,
        confidence=0.75,
    ).model_dump(mode="json")


def test_extract_risk_decision_uses_tool_registry(monkeypatch) -> None:
    registry = object()
    captured: dict[str, Any] = {}

    def fake_assess_plan_step(step, tool_registry=None, incident=None):
        captured["tool_registry"] = tool_registry
        captured["incident"] = incident
        return replanner_module.RiskControlDecision(
            action="query",
            tool_name=step.tool_name,
            step_id=step.step_id,
            policy="allow",
            allowed=True,
            need_approval=False,
            reason="allowed",
        )

    monkeypatch.setattr(replanner_module, "create_default_tool_registry", lambda _tools: registry)
    monkeypatch.setattr(replanner_module, "assess_plan_step", fake_assess_plan_step)
    state = create_initial_aiops_state("diagnose", session_id="risk-registry")
    state["current_plan"] = [
        PlanStep(step_id="step-1", tool_name="query_metrics").model_dump(mode="json")
    ]

    assert replanner_module._extract_risk_decision(state) is None
    assert captured["tool_registry"] is registry
    assert captured["incident"] == state["incident"]


async def fake_generate_response_with_analysis(state, analysis):
    return {"response": f"report: {analysis.decision}"}


class FakeStructuredLLM:
    def with_structured_output(self, _schema: Any) -> "FakeStructuredLLM":
        return self


class FakeReplannerPrompt:
    def __init__(self, decision: Any) -> None:
        self.decision = decision
        self.payload: dict[str, Any] | None = None

    def __or__(self, _structured_llm: Any) -> "FakeReplannerChain":
        return FakeReplannerChain(self)


class FakeReplannerChain:
    def __init__(self, prompt: FakeReplannerPrompt) -> None:
        self.prompt = prompt

    async def ainvoke(self, payload: dict[str, Any]) -> Any:
        self.prompt.payload = payload
        return self.prompt.decision


@pytest.mark.asyncio
async def test_replanner_adds_missing_evidence_steps_when_plan_is_empty() -> None:
    state = create_initial_aiops_state(
        "order-service Redis connection timeout",
        session_id="replanner-add-steps",
    )
    state["incident"]["service_name"] = "order-service"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_logs",
            {"summary": "Redis connection timeout in logs"},
            "s1",
        )
    ]

    update = await replanner_module.replanner(state)

    tool_names = [step["tool_name"] for step in update["current_plan"]]
    assert "query_metrics" in tool_names
    assert "query_redis_status" in tool_names
    assert len(update["plan"]) == len(update["current_plan"])
    assert update["plan"][0].startswith("[")
    assert update["hypotheses"]
    assert update["evidence_analysis"]["decision"] == "add_steps"
    assert "query_metrics" in update["evidence_analysis"]["missing_evidence"]


@pytest.mark.asyncio
async def test_replanner_retries_failed_tool_without_calling_llm(monkeypatch) -> None:
    monkeypatch.setattr(replanner_module.config, "aiops_replanner_llm_enabled", True)
    monkeypatch.setattr(
        replanner_module,
        "_create_llm",
        lambda: pytest.fail("retry decisions must not call the Replanner LLM"),
    )
    state = create_initial_aiops_state(
        "order-service Redis connection timeout",
        session_id="replanner-retry",
    )
    state["tool_call_records"] = [
        {
            "trace_id": state["trace_id"],
            "incident_id": state["incident"]["incident_id"],
            "step_id": "s3",
            "tool_name": "query_redis_status",
            "input_args": {"service_name": "order-service"},
            "status": "failed",
            "error_message": "redis backend unavailable",
        }
    ]

    update = await replanner_module.replanner(state)

    assert update["current_plan"][0]["tool_name"] == "query_redis_status"
    assert update["current_plan"][0]["step_id"] == "s3-retry"
    assert update["current_plan"][0]["retry_count"] == 1
    assert update["plan"][0].startswith("[s3-retry]")


@pytest.mark.asyncio
async def test_replanner_retry_preserves_remaining_risk_action(monkeypatch) -> None:
    monkeypatch.setattr(replanner_module.config, "aiops_replanner_llm_enabled", False)
    state = create_initial_aiops_state(
        "order-service Redis connection timeout",
        session_id="replanner-retry-preserves-risk",
    )
    state["incident"]["environment"] = "prod"
    state["current_plan"] = [
        PlanStep(
            step_id="s-risk",
            tool_name="restart_service",
            purpose="重启生产服务",
            input_args={"service_name": "order-service"},
            expected_evidence="服务恢复",
            risk_level="medium",
        ).model_dump(mode="json")
    ]
    state["plan"] = ["legacy risk step"]
    state["tool_call_records"] = [
        {
            "trace_id": state["trace_id"],
            "incident_id": state["incident"]["incident_id"],
            "step_id": "s3",
            "tool_name": "query_redis_status",
            "input_args": {"service_name": "order-service"},
            "status": "failed",
            "error_message": "redis backend unavailable",
        }
    ]

    update = await replanner_module.replanner(state)

    assert [step["tool_name"] for step in update["current_plan"]] == [
        "query_redis_status",
        "restart_service",
    ]
    assert update["current_plan"][1]["step_id"] == "s-risk"
    assert len(update["plan"]) == 2


@pytest.mark.asyncio
async def test_replanner_uses_enabled_llm_structured_decision(monkeypatch) -> None:
    llm_step = PlanStep(
        step_id="llm-redis",
        tool_name="query_redis_status",
        purpose="补查 order-service 到 Redis 的调用链错误和耗时",
        input_args={"service_name": "order-service", "time_range": "10m"},
        expected_evidence="确认 Redis 调用链是否出现超时或错误",
        risk_level="low",
    )
    fake_prompt = FakeReplannerPrompt(
        replanner_module.ReplanDecision(
            decision="add_steps",
            reason="日志已提示 Redis timeout，需要补充调用链证据",
            new_steps=[llm_step],
        )
    )
    monkeypatch.setattr(replanner_module.config, "aiops_replanner_llm_enabled", True)
    monkeypatch.setattr(replanner_module, "_create_llm", lambda: FakeStructuredLLM())
    monkeypatch.setattr(replanner_module, "replanner_prompt", fake_prompt)

    state = create_initial_aiops_state(
        "order-service Redis connection timeout",
        session_id="replanner-llm-add-steps",
    )
    state["incident"]["service_name"] = "order-service"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_logs",
            {"summary": "Redis connection timeout in logs"},
            "s1",
        )
    ]

    update = await replanner_module.replanner(state)

    assert update["current_plan"][0]["tool_name"] == "query_redis_status"
    assert update["current_plan"][0]["step_id"] == "llm-redis"
    assert update["current_plan"][0]["status"] == "pending"
    assert update["evidence_analysis"]["decision"] == "add_steps"
    assert fake_prompt.payload is not None
    assert "query_redis_status" in fake_prompt.payload["tools_description"]
    assert any(
        "Evidence Analyzer 摘要" in content for _, content in fake_prompt.payload["messages"]
    )


@pytest.mark.asyncio
async def test_replanner_add_steps_preserves_remaining_risk_action(monkeypatch) -> None:
    llm_step = PlanStep(
        step_id="llm-metrics",
        tool_name="query_metrics",
        purpose="补查 order-service 指标",
        input_args={"service_name": "order-service"},
        expected_evidence="补充延迟和错误率证据",
        risk_level="low",
    )
    fake_prompt = FakeReplannerPrompt(
        replanner_module.ReplanDecision(
            decision="add_steps",
            reason="补充指标证据",
            new_steps=[llm_step],
        )
    )
    monkeypatch.setattr(replanner_module.config, "aiops_replanner_llm_enabled", True)
    monkeypatch.setattr(replanner_module, "_create_llm", lambda: FakeStructuredLLM())
    monkeypatch.setattr(replanner_module, "replanner_prompt", fake_prompt)
    monkeypatch.setattr(
        replanner_module,
        "analyze_evidence",
        lambda _: EvidenceAnalysis(
            decision="continue_investigation",
            reason="仍有剩余计划",
            evidence_sufficient=False,
        ),
    )
    state = create_initial_aiops_state(
        "order-service Redis connection timeout",
        session_id="replanner-add-preserves-risk",
    )
    state["incident"]["environment"] = "prod"
    state["current_plan"] = [
        PlanStep(
            step_id="s-risk",
            tool_name="restart_service",
            purpose="重启生产服务",
            input_args={"service_name": "order-service"},
            expected_evidence="服务恢复",
            risk_level="medium",
        ).model_dump(mode="json")
    ]
    state["plan"] = ["legacy risk step"]

    update = await replanner_module.replanner(state)

    assert [step["tool_name"] for step in update["current_plan"]] == [
        "query_metrics",
        "restart_service",
    ]
    assert update["current_plan"][1]["step_id"] == "s-risk"
    assert len(update["plan"]) == 2


@pytest.mark.asyncio
async def test_replanner_add_steps_preserves_legacy_only_remaining_plan(monkeypatch) -> None:
    monkeypatch.setattr(replanner_module.config, "aiops_replanner_llm_enabled", False)
    monkeypatch.setattr(
        replanner_module,
        "analyze_evidence",
        lambda _: EvidenceAnalysis(
            decision="add_steps",
            reason="需要补充指标证据",
            recommended_steps=[
                PlanStep(
                    step_id="s-new",
                    tool_name="query_metrics",
                    purpose="补查指标",
                    input_args={"service_name": "order-service"},
                    expected_evidence="延迟和错误率证据",
                )
            ],
        ),
    )
    state = create_initial_aiops_state(
        "order-service timeout",
        session_id="replanner-add-preserves-legacy",
    )
    state["plan"] = ["legacy diagnostic step"]

    update = await replanner_module.replanner(state)

    assert [step["tool_name"] for step in update["current_plan"]] == [
        "query_metrics",
        "manual_analysis",
    ]
    assert update["current_plan"][1]["purpose"] == "legacy diagnostic step"
    assert len(update["plan"]) == 2


def test_replanned_steps_do_not_duplicate_existing_queue_entries() -> None:
    state = create_initial_aiops_state(
        "order-service timeout",
        session_id="replanner-deduplicate-queue",
    )
    existing_step = PlanStep(
        step_id="existing-metrics",
        tool_name="query_metrics",
        purpose="检查指标",
        input_args={"service_name": "order-service"},
        expected_evidence="延迟和错误率证据",
    )
    state["current_plan"] = [existing_step.model_dump(mode="json")]
    state["plan"] = ["legacy metrics"]
    new_step = existing_step.model_copy(update={"step_id": "new-metrics"})

    update = replanner_module._steps_with_remaining_plan_state_update(state, [new_step])

    assert len(update["current_plan"]) == 1
    assert update["current_plan"][0]["step_id"] == "new-metrics"
    assert len(update["plan"]) == 1


def test_replanned_steps_reassign_colliding_step_ids() -> None:
    state = create_initial_aiops_state(
        "order-service timeout",
        session_id="replanner-unique-step-ids",
    )
    state["current_plan"] = [
        PlanStep(
            step_id="same-id",
            tool_name="query_logs",
            purpose="检查日志",
            input_args={"service_name": "order-service"},
            expected_evidence="错误日志",
        ).model_dump(mode="json")
    ]
    new_step = PlanStep(
        step_id="same-id",
        tool_name="query_metrics",
        purpose="检查指标",
        input_args={"service_name": "order-service"},
        expected_evidence="延迟和错误率",
    )

    update = replanner_module._steps_with_remaining_plan_state_update(state, [new_step])

    step_ids = [step["step_id"] for step in update["current_plan"]]
    assert len(step_ids) == len(set(step_ids))


def test_replanned_steps_do_not_repeat_already_executed_diagnostics() -> None:
    state = create_initial_aiops_state(
        "order-service timeout",
        session_id="replanner-deduplicate-executed",
    )
    executed_step = PlanStep(
        step_id="done-metrics",
        tool_name="query_metrics",
        purpose="检查指标",
        input_args={"service_name": "order-service"},
        expected_evidence="延迟和错误率",
        status="success",
    )
    state["executed_steps"] = [executed_step.model_dump(mode="json")]
    repeated_step = executed_step.model_copy(
        update={"step_id": "repeat-metrics", "status": "pending"}
    )

    update = replanner_module._steps_with_remaining_plan_state_update(state, [repeated_step])

    assert update == {"current_plan": [], "plan": []}


def test_retry_steps_can_repeat_failed_execution_identity() -> None:
    state = create_initial_aiops_state(
        "order-service timeout",
        session_id="replanner-allow-explicit-retry",
    )
    failed_step = PlanStep(
        step_id="failed-redis",
        tool_name="query_redis_status",
        purpose="检查 Redis",
        input_args={"service_name": "order-service"},
        expected_evidence="Redis 状态",
        status="failed",
    )
    state["executed_steps"] = [failed_step.model_dump(mode="json")]
    retry_step = failed_step.model_copy(
        update={"step_id": "failed-redis-retry", "status": "pending", "retry_count": 1}
    )

    update = replanner_module._steps_with_remaining_plan_state_update(
        state,
        [retry_step],
        allow_executed_duplicates=True,
    )

    assert [step["step_id"] for step in update["current_plan"]] == ["failed-redis-retry"]


@pytest.mark.asyncio
async def test_replanner_escalates_when_add_steps_only_repeats_executed_work(
    monkeypatch,
) -> None:
    repeated_step = PlanStep(
        step_id="repeat-metrics",
        tool_name="query_metrics",
        purpose="检查指标",
        input_args={"service_name": "order-service"},
        expected_evidence="延迟和错误率",
    )
    monkeypatch.setattr(replanner_module.config, "aiops_replanner_llm_enabled", False)
    monkeypatch.setattr(
        replanner_module,
        "analyze_evidence",
        lambda _: EvidenceAnalysis(
            decision="add_steps",
            reason="错误地重复相同指标查询",
            recommended_steps=[repeated_step],
        ),
    )
    state = create_initial_aiops_state(
        "order-service timeout",
        session_id="replanner-repeated-work-escalation",
    )
    state["executed_steps"] = [
        repeated_step.model_copy(
            update={"step_id": "done-metrics", "status": "success"}
        ).model_dump(mode="json")
    ]

    update = await replanner_module.replanner(state)

    assert update["report"]["status"] == "escalated"
    assert "未产生新的可执行步骤" in update["response"]
    assert update["errors"] == ["重规划未产生新的可执行步骤，停止重复排查并升级人工处理"]


@pytest.mark.asyncio
async def test_replanner_rejects_unsafe_llm_steps_and_falls_back(monkeypatch) -> None:
    fake_prompt = FakeReplannerPrompt(
        replanner_module.ReplanDecision(
            decision="add_steps",
            reason="错误地建议删除 Pod",
            new_steps=[
                PlanStep(
                    step_id="unsafe",
                    tool_name="delete_pod",
                    purpose="删除生产 Pod",
                    input_args={"pod": "order-service-0"},
                    expected_evidence="Pod 被删除",
                    risk_level="high",
                )
            ],
        )
    )
    monkeypatch.setattr(replanner_module.config, "aiops_replanner_llm_enabled", True)
    monkeypatch.setattr(replanner_module, "_create_llm", lambda: FakeStructuredLLM())
    monkeypatch.setattr(replanner_module, "replanner_prompt", fake_prompt)

    state = create_initial_aiops_state(
        "order-service Redis connection timeout",
        session_id="replanner-llm-unsafe-fallback",
    )
    state["incident"]["service_name"] = "order-service"
    state["incident"]["environment"] = "prod"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_logs",
            {"summary": "Redis connection timeout in logs"},
            "s1",
        )
    ]

    update = await replanner_module.replanner(state)

    tool_names = [step["tool_name"] for step in update["current_plan"]]
    assert "delete_pod" not in tool_names
    assert "query_metrics" in tool_names
    assert "query_redis_status" in tool_names


@pytest.mark.asyncio
async def test_replanner_generates_report_when_evidence_is_sufficient(monkeypatch) -> None:
    monkeypatch.setattr(
        replanner_module,
        "_generate_response_with_analysis",
        fake_generate_response_with_analysis,
    )
    state = create_initial_aiops_state(
        "order-service Redis connection timeout and 5xx",
        session_id="replanner-report",
    )
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_metrics",
            {"summary": "P95=3250ms, 5xx=8.20%", "p95_latency_ms": {"status": "high"}},
            "s1",
        ),
        evidence_from_tool(
            "query_logs",
            {"summary": "Redis connection timeout in logs"},
            "s2",
        ),
        evidence_from_tool(
            "query_redis_status",
            {
                "summary": "connected_clients=9940/10000",
                "connected_clients": 9940,
                "maxclients": 10000,
                "client_usage_ratio": 0.994,
            },
            "s3",
        ),
    ]

    update = await replanner_module.replanner(state)

    assert update["response"] == "report: generate_report"
    assert update["hypotheses"]
    assert "Redis" in update["final_diagnosis"]


@pytest.mark.asyncio
async def test_replanner_checks_remaining_risk_before_generating_report(monkeypatch) -> None:
    state = create_initial_aiops_state(
        "order-service Redis connection timeout and 5xx",
        session_id="replanner-risk-before-report",
    )
    state["incident"]["environment"] = "prod"
    state["current_plan"] = [
        PlanStep(
            step_id="s9",
            tool_name="restart_service",
            purpose="重启生产服务以释放异常连接",
            input_args={"service_name": "order-service"},
            expected_evidence="服务重启后错误率恢复",
            risk_level="medium",
        ).model_dump(mode="json")
    ]
    monkeypatch.setattr(
        replanner_module,
        "analyze_evidence",
        lambda _: EvidenceAnalysis(
            decision="generate_report",
            reason="证据已足够定位根因",
            hypotheses=["Redis 连接数达到 maxclients"],
            evidence_sufficient=True,
            confidence=0.85,
        ),
    )

    update = await replanner_module.replanner(state)

    assert update["risk_assessment"]["policy"] == "approval_required"
    assert update["pending_approval"]["tool_name"] == "restart_service"
    assert update["response"].startswith("# AIOps 诊断已暂停")


@pytest.mark.asyncio
async def test_replanner_max_steps_fails_closed_for_legacy_remaining_plan() -> None:
    state = create_initial_aiops_state(
        "diagnose stubborn incident",
        session_id="replanner-max-steps",
    )
    state["past_steps"] = [
        (f"step-{index}", "result") for index in range(replanner_module.MAX_STEPS)
    ]
    state["plan"] = ["still more work"]

    update = await replanner_module.replanner(state)

    assert update["risk_assessment"]["policy"] == "forbidden"
    assert "plan:legacy-unassessed" in update["risk_assessment"]["matched_rules"]
    assert update["pending_approval"] is None


@pytest.mark.asyncio
async def test_replanner_max_steps_counts_executions_not_administrative_history(
    monkeypatch,
) -> None:
    monkeypatch.setattr(replanner_module.config, "aiops_replanner_llm_enabled", False)
    monkeypatch.setattr(
        replanner_module,
        "analyze_evidence",
        lambda _: EvidenceAnalysis(
            decision="continue_investigation",
            reason="仍有一步可执行",
            evidence_sufficient=False,
        ),
    )
    state = create_initial_aiops_state(
        "continue bounded diagnosis",
        session_id="replanner-execution-count",
    )
    state["past_steps"] = [
        (f"administrative-{index}", "postponed") for index in range(replanner_module.MAX_STEPS)
    ]
    state["executed_steps"] = [
        PlanStep(step_id="done-1", tool_name="query_metrics", status="success").model_dump(
            mode="json"
        )
    ]
    state["current_plan"] = [
        PlanStep(step_id="next-1", tool_name="query_logs").model_dump(mode="json")
    ]
    state["plan"] = ["legacy next"]

    update = await replanner_module.replanner(state)

    assert "response" not in update


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "risk_level", "expected_policy"),
    [
        ("restart_service", "medium", "approval_required"),
        ("delete_pod", "high", "forbidden"),
    ],
)
async def test_replanner_max_steps_applies_risk_gate_before_terminal_response(
    tool_name: str,
    risk_level: str,
    expected_policy: str,
) -> None:
    state = create_initial_aiops_state(
        "diagnose stubborn production incident",
        session_id=f"replanner-max-steps-{tool_name}",
    )
    state["incident"]["environment"] = "prod"
    state["past_steps"] = [
        (f"step-{index}", "result") for index in range(replanner_module.MAX_STEPS)
    ]
    state["current_plan"] = [
        PlanStep(
            step_id="s-risk",
            tool_name=tool_name,
            purpose=f"执行生产动作 {tool_name}",
            input_args={"service_name": "order-service"},
            expected_evidence="生产动作结果",
            risk_level=risk_level,
        ).model_dump(mode="json")
    ]
    state["plan"] = ["legacy risk step"]

    update = await replanner_module.replanner(state)

    assert update["risk_assessment"]["policy"] == expected_policy
    if expected_policy == "approval_required":
        assert update["pending_approval"]["tool_name"] == tool_name
        assert "等待人工审批" in update["response"]
    else:
        assert update["pending_approval"] is None
        assert "已拦截危险动作" in update["response"]


@pytest.mark.asyncio
async def test_replanner_request_approval_writes_structured_state(monkeypatch) -> None:
    state = create_initial_aiops_state(
        "order-service Redis connection timeout",
        session_id="replanner-approval",
    )
    state["current_plan"] = [
        PlanStep(
            step_id="s9",
            tool_name="manual_remediation",
            purpose="临时调整 Redis maxclients 配置",
            expected_evidence="变更后连接数恢复正常",
            risk_level="high",
        ).model_dump(mode="json")
    ]

    monkeypatch.setattr(
        replanner_module,
        "analyze_evidence",
        lambda _: EvidenceAnalysis(
            decision="request_approval",
            reason="该处置动作会修改生产 Redis 配置",
            hypotheses=["Redis 连接数达到 maxclients"],
        ),
    )

    update = await replanner_module.replanner(state)

    assert update["risk_assessment"]["risk_level"] == "high"
    assert update["risk_assessment"]["need_approval"] is True
    assert update["pending_approval"]["incident_id"] == state["incident"]["incident_id"]
    assert update["pending_approval"]["risk_level"] == "high"
    assert update["pending_approval"]["status"] == "pending"
    assert update["pending_approval"]["step_id"] == "s9"
    assert "等待人工审批" in update["response"]
    assert "Agent 不会自动执行生产变更" in update["response"]


def test_extract_risk_decision_fails_closed_for_invalid_structured_step() -> None:
    state = create_initial_aiops_state(
        "diagnose invalid remaining plan",
        session_id="replanner-invalid-risk-step",
    )
    state["current_plan"] = [
        {
            "step_id": "broken-risk",
            "tool_name": "restart_service",
            "purpose": "重启生产服务",
            "input_args": [],
        }
    ]

    decision = replanner_module._extract_risk_decision(state)

    assert decision is not None
    assert decision.policy == "forbidden"
    assert "plan:invalid-step" in decision.matched_rules


def test_extract_risk_decision_fails_closed_for_legacy_text_only_plan() -> None:
    state = create_initial_aiops_state(
        "restart production service",
        session_id="replanner-legacy-risk-plan",
    )
    state["plan"] = ["restart production service"]

    decision = replanner_module._extract_risk_decision(state)

    assert decision is not None
    assert decision.policy == "forbidden"
    assert decision.risk_level == "high"
    assert "plan:legacy-unassessed" in decision.matched_rules


@pytest.mark.asyncio
async def test_replanner_blocks_legacy_text_only_plan(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        replanner_module,
        "analyze_evidence",
        lambda _: EvidenceAnalysis(
            decision="continue_investigation",
            reason="remaining legacy action",
        ),
    )
    monkeypatch.setattr(
        replanner_module.trace_service,
        "record_risk_decision",
        lambda **_: None,
    )
    monkeypatch.setattr(
        replanner_module,
        "_with_generated_report",
        lambda _state, update, status: {**update, "report_status": status},
    )
    state = create_initial_aiops_state(
        "restart production service",
        session_id="replanner-legacy-plan-block",
    )
    state["plan"] = ["restart production service"]

    update = await replanner_module.replanner(state)

    assert update["risk_assessment"]["policy"] == "forbidden"
    assert update["pending_approval"] is None
    assert update["report_status"] == "blocked"


@pytest.mark.asyncio
async def test_generate_response_budgets_long_context_and_keeps_latest_failure() -> None:
    class CapturingChain:
        def __init__(self) -> None:
            self.payload: dict[str, Any] | None = None

        async def ainvoke(self, payload: dict[str, Any]) -> dict[str, str]:
            self.payload = payload
            return {"response": "bounded"}

    class CapturingPrompt:
        def __init__(self, chain: CapturingChain) -> None:
            self.chain = chain

        def __or__(self, _llm: Any) -> CapturingChain:
            return self.chain

    chain = CapturingChain()
    original_prompt = replanner_module.response_prompt
    replanner_module.response_prompt = CapturingPrompt(chain)
    try:
        state = create_initial_aiops_state(
            "x" * 10_000,
            session_id="replanner-bounded-final-response",
        )
        state["past_steps"] = [
            ("old step", "old result " * 1000),
            ("latest step", "LATEST_FAILED_RESULT"),
        ]
        state["gathered_evidence"] = [
            {
                "source_tool": "query_logs",
                "step_id": f"s-{index}",
                "summary": "old evidence " * 100,
                "raw_data": {"status": "success"},
            }
            for index in range(20)
        ] + [
            {
                "source_tool": "query_redis_status",
                "step_id": "latest",
                "summary": "LATEST_FAILED_EVIDENCE",
                "raw_data": {"status": "failed"},
            }
        ]
        state["tool_call_records"] = [
            {
                "tool_name": "query_logs",
                "step_id": f"s-{index}",
                "status": "success",
                "error_message": "",
            }
            for index in range(100)
        ] + [
            {
                "tool_name": "query_redis_status",
                "step_id": "latest",
                "status": "failed",
                "error_message": "LATEST_TOOL_FAILURE",
            }
        ]

        update = await replanner_module._generate_response(state, FakeStructuredLLM())
    finally:
        replanner_module.response_prompt = original_prompt

    assert update == {"response": "bounded"}
    assert chain.payload is not None
    messages = chain.payload["messages"]
    for _, content in messages[:-1]:
        assert len(content) <= replanner_module.RESPONSE_CONTEXT_CHAR_LIMIT + 32
    assert any("LATEST_FAILED_RESULT" in content for _, content in messages)
    assert any("LATEST_FAILED_EVIDENCE" in content for _, content in messages)
    assert any("LATEST_TOOL_FAILURE" in content for _, content in messages)
