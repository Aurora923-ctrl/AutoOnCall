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
async def test_replanner_max_steps_forces_response(monkeypatch) -> None:
    monkeypatch.setattr(
        replanner_module,
        "_generate_response_with_analysis",
        fake_generate_response_with_analysis,
    )
    state = create_initial_aiops_state(
        "diagnose stubborn incident",
        session_id="replanner-max-steps",
    )
    state["past_steps"] = [
        (f"step-{index}", "result") for index in range(replanner_module.MAX_STEPS)
    ]
    state["plan"] = ["still more work"]

    update = await replanner_module.replanner(state)

    assert update["response"].startswith("report:")


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
