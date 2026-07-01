"""Regression tests for the first industrial AIOps model upgrade."""

import pytest
from pydantic import ValidationError

from app.agent.aiops import create_initial_aiops_state
from app.agent.aiops.planner import _build_planner_retrieval_query
from app.agent.aiops.state import normalize_plan_state_update, remaining_plan_state_update
from app.models.aiops import AIOpsRequest, AIOpsResumeRequest
from app.models.approval import ApprovalRequest, RiskAssessment
from app.models.evidence import Evidence
from app.models.incident import Incident
from app.models.plan import PlanStep
from app.models.report import DiagnosisReport
from app.models.trace import ToolCallRecord, TraceEvent
from app.services.aiops_service import _build_incident_diagnosis_input
from app.services.incident_state_builder import build_incident_state_from_state


def test_aiops_request_keeps_legacy_session_only_payload() -> None:
    request = AIOpsRequest(session_id="session-123")

    assert request.session_id == "session-123"
    assert request.incident is None


def test_aiops_request_defaults_to_no_shared_session() -> None:
    request = AIOpsRequest()

    assert request.session_id is None
    assert request.incident is None


@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": ""},
        {"session_id": "s" * 129},
    ],
)
def test_aiops_request_rejects_invalid_session_id_boundaries(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        AIOpsRequest(**payload)


def test_aiops_resume_request_rejects_invalid_ids() -> None:
    with pytest.raises(ValidationError):
        AIOpsResumeRequest(session_id="s" * 129)
    with pytest.raises(ValidationError):
        AIOpsResumeRequest(approval_id="")


def test_initial_aiops_state_generates_unique_session_when_missing() -> None:
    first = create_initial_aiops_state("diagnose current alerts")
    second = create_initial_aiops_state("diagnose current alerts")

    assert first["session_id"].startswith("session-")
    assert second["session_id"].startswith("session-")
    assert first["session_id"] != second["session_id"]
    assert first["session_id"] != "default"


def test_aiops_request_accepts_optional_structured_incident() -> None:
    request = AIOpsRequest(
        session_id="session-redis",
        incident={
            "title": "order-service Redis timeout",
            "service_name": "order-service",
            "severity": "P1",
            "symptom": "5xx and Redis connection timeout",
            "environment": "prod",
        },
    )

    assert request.incident is not None
    assert request.incident.service_name == "order-service"
    assert request.incident.severity == "P1"


def test_incident_diagnosis_input_includes_structured_context() -> None:
    incident = Incident(
        title="order-service Redis maxclients exhausted",
        service_name="order-service",
        severity="P1",
        symptom="Redis connection timeout and 5xx spike",
        environment="prod",
        raw_alert={"alertname": "RedisMaxClients", "maxclients": 10000},
    )

    rendered = _build_incident_diagnosis_input("生成诊断报告", incident)

    assert "不要只按通用巡检处理" in rendered
    assert "order-service Redis maxclients exhausted" in rendered
    assert "service_name: order-service" in rendered
    assert "Redis connection timeout and 5xx spike" in rendered
    assert '"alertname": "RedisMaxClients"' in rendered
    assert "生成诊断报告" in rendered


def test_planner_retrieval_query_focuses_on_incident_not_report_template() -> None:
    query = _build_planner_retrieval_query(
        "生成包含摘要、根因、风险、修复建议的 Markdown 诊断报告",
        {
            "title": "checkout-service Redpanda consumer lag",
            "service_name": "checkout-service",
            "severity": "P2",
            "symptom": "订单消息积压，怀疑 Redpanda/Kafka topic 或 partition 异常",
            "environment": "prod",
            "raw_alert": {
                "alertname": "RedpandaConsumerLagHigh",
                "topic": "redpanda-checkout",
                "consumer_lag": 128400,
            },
        },
    )

    assert "checkout-service" in query
    assert "RedpandaConsumerLagHigh" in query
    assert "redpanda-checkout" in query
    assert "consumer_lag=128400" in query
    assert "Markdown 诊断报告" not in query


def test_aiops_domain_models_are_json_dumpable() -> None:
    incident = Incident(service_name="order-service", symptom="high 5xx")
    step = PlanStep(
        step_id="s1",
        tool_name="query_metrics",
        purpose="Check latency and error rate",
        expected_evidence="P95 and 5xx trend",
    )
    evidence = Evidence(
        source_tool="query_metrics",
        step_id=step.step_id,
        summary="P95 increased above threshold",
        evidence_type="metric",
        stance="supporting",
        confidence_reason="指标阈值命中",
        confidence=0.8,
    )
    risk = RiskAssessment(
        risk_level="high",
        action="restart service",
        reason="Impacts production traffic",
        need_approval=True,
    )
    approval = ApprovalRequest(
        incident_id=incident.incident_id,
        action=risk.action,
        risk_level=risk.risk_level,
        reason=risk.reason,
    )
    tool_call = ToolCallRecord(
        trace_id="trace-1",
        incident_id=incident.incident_id,
        step_id=step.step_id,
        tool_name=step.tool_name,
        status="success",
    )
    trace = TraceEvent(
        trace_id="trace-1",
        incident_id=incident.incident_id,
        node_name="executor",
        step_id=step.step_id,
    )
    report = DiagnosisReport(
        incident_id=incident.incident_id,
        summary="Redis timeout caused user-facing latency",
        evidence=[evidence.model_dump(mode="json")],
        confidence=0.7,
    )

    for model in [incident, step, evidence, risk, approval, tool_call, trace, report]:
        dumped = model.model_dump(mode="json")
        assert isinstance(dumped, dict)
        assert dumped

    assert evidence.evidence_type == "metric"
    assert evidence.stance == "supporting"
    assert evidence.confidence_reason == "指标阈值命中"


def test_initial_aiops_state_is_backward_compatible() -> None:
    state = create_initial_aiops_state("diagnose current alerts", session_id="smoke")

    assert state["input"] == "diagnose current alerts"
    assert state["plan"] == []
    assert state["past_steps"] == []
    assert state["response"] == ""
    assert state["incident"]["raw_alert"]["session_id"] == "smoke"
    assert state["incident"]["status"] == "investigating"
    assert state["trace_id"].startswith("trace-")
    assert state["current_plan"] == []
    assert state["tool_call_records"] == []
    assert state["gathered_evidence"] == []
    assert state["evidence_analysis"] is None
    assert state["errors"] == []


def test_incident_state_from_state_uses_report_approval_id_when_pending_is_cleared() -> None:
    state = {
        "incident": {
            "incident_id": "inc-approval-resumed",
            "title": "order-service Redis timeout",
            "service_name": "order-service",
            "severity": "P1",
            "environment": "prod",
        },
        "trace_id": "trace-approval-resumed",
        "pending_approval": None,
        "report": {
            "report_id": "rpt-1",
            "approval_status": "approved",
            "approval_decision": {"approval_id": "apr-resumed"},
            "manual_action_required": True,
        },
    }

    incident_state = build_incident_state_from_state(
        state=state,
        status="approval_resumed",
        session_id="session-resumed",
    )

    assert incident_state.latest_approval_id == "apr-resumed"
    assert incident_state.approval_status == "approved"


def test_plan_state_helpers_keep_canonical_and_legacy_plan_in_sync() -> None:
    steps = [
        PlanStep(
            step_id="s1",
            tool_name="query_metrics",
            purpose="检查指标",
            input_args={"service_name": "order-service"},
            expected_evidence="指标证据",
        ),
        PlanStep(
            step_id="s2",
            tool_name="query_logs",
            purpose="检查日志",
            input_args={"service_name": "order-service"},
            expected_evidence="日志证据",
        ),
    ]

    update = normalize_plan_state_update(steps)

    assert [step["step_id"] for step in update["current_plan"]] == ["s1", "s2"]
    assert update["plan"][0].startswith("[s1] 使用 query_metrics")

    remaining = remaining_plan_state_update(update["current_plan"], update["plan"])

    assert [step["step_id"] for step in remaining["current_plan"]] == ["s2"]
    assert remaining["plan"] == [update["plan"][1]]
