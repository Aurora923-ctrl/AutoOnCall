"""Tests for trace events emitted by AIOps workflow components."""

import importlib
from typing import Any

import pytest

from app.agent.aiops import create_initial_aiops_state
from app.config import config
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.approval import ApprovalRequest
from app.models.plan import PlanStep
from app.models.report import DiagnosisReport
from app.services.aiops_service import AIOpsService
from app.services.approval_service import ApprovalService
from app.services.report_generator import ReportGenerator
from app.services.sqlite_store import AIOpsSQLiteStore
from app.services.trace_service import TraceService

executor_module = importlib.import_module("app.agent.aiops.executor")
aiops_service_module = importlib.import_module("app.services.aiops_service")
approval_service_module = importlib.import_module("app.services.approval_service")


class EmptyMCPClient:
    async def get_tools(self) -> list[Any]:
        return []


async def fake_get_mcp_client_with_retry() -> EmptyMCPClient:
    return EmptyMCPClient()


def state_with_step(step: PlanStep) -> dict[str, Any]:
    state = create_initial_aiops_state(
        "diagnose order-service Redis timeout",
        session_id="trace-events-test",
    )
    state["incident"]["environment"] = "prod"
    state["current_plan"] = [step.model_dump(mode="json")]
    state["plan"] = [step.purpose]
    return state


@pytest.mark.asyncio
async def test_executor_records_tool_call_trace(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "aiops_mock_fallback_enabled", True)
    trace_store = TraceService(tmp_path / "traces.db")
    monkeypatch.setattr(executor_module, "trace_service", trace_store)
    monkeypatch.setattr(
        executor_module,
        "get_mcp_client_with_retry",
        fake_get_mcp_client_with_retry,
    )
    step = PlanStep(
        step_id="s1",
        tool_name="query_redis_status",
        purpose="检查 Redis 状态",
        input_args={"service_name": "order-service"},
        expected_evidence="Redis 连接数证据",
    )
    state = state_with_step(step)

    await executor_module.executor(state)

    events = trace_store.list_events(
        incident_id=state["incident"]["incident_id"],
        event_type="tool_call",
    )
    assert len(events) == 1
    assert events[0].tool_name == "query_redis_status"
    assert events[0].step_id == "s1"
    assert events[0].status == "failed"
    assert events[0].metadata["data_source"] == "not_configured"


@pytest.mark.asyncio
async def test_executor_records_risk_and_approval_trace(monkeypatch, tmp_path) -> None:
    trace_store = TraceService(tmp_path / "traces.db")
    approval_store = ApprovalService(tmp_path / "approvals.db")
    monkeypatch.setattr(executor_module, "trace_service", trace_store)
    monkeypatch.setattr(approval_service_module, "trace_service", trace_store)
    monkeypatch.setattr(executor_module, "approval_service", approval_store)
    monkeypatch.setattr(
        executor_module,
        "get_mcp_client_with_retry",
        fake_get_mcp_client_with_retry,
    )
    step = PlanStep(
        step_id="s2",
        tool_name="restart_service",
        purpose="重启生产服务以释放异常连接",
        input_args={"service_name": "order-service"},
        expected_evidence="服务重启完成",
        risk_level="medium",
    )
    state = state_with_step(step)

    update = await executor_module.executor(state)

    assert update["pending_approval"]["status"] == "pending"
    incident_id = state["incident"]["incident_id"]
    assert trace_store.list_events(incident_id=incident_id, event_type="risk_decision")
    approval_events = trace_store.list_events(
        incident_id=incident_id, event_type="approval_request"
    )
    assert approval_events
    assert approval_events[0].metadata["approval_id"] == update["pending_approval"]["approval_id"]


def test_sse_payload_can_attach_trace_event(tmp_path) -> None:
    trace_store = TraceService(tmp_path / "traces.db")
    trace_event = trace_store.record_node_event(
        trace_id="trace-sse",
        incident_id="inc-sse",
        node_name="planner",
        node_output={"plan": ["step 1"]},
    )

    payload = aiops_service_module._attach_trace_event({"type": "plan"}, trace_event)

    assert payload["trace_id"] == "trace-sse"
    assert payload["trace_event_id"] == trace_event.event_id
    assert payload["trace_event"]["event_type"] == "node"


@pytest.mark.asyncio
async def test_resume_after_approval_uses_persisted_report_when_checkpoint_is_missing(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-resume-persisted"
    trace_id = "trace-resume-persisted"
    trace_store = TraceService(tmp_path / "resume.db")
    report_store = ReportGenerator(tmp_path / "resume.db")
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(tmp_path / "resume.db")
    approval = ApprovalRequest(
        incident_id=incident_id,
        action="人工调整 Redis maxclients",
        risk_level="high",
        reason="生产配置变更需要审批",
        status="approved",
        decided_by="pytest",
        decision_reason="approved for manual mitigation",
        metadata={"trace_id": trace_id},
    )
    report_store.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            title="order-service AIOps 诊断报告",
            service_name="order-service",
            severity="P1",
            environment="prod",
            status="approval_approved",
            summary="Redis maxclients 接近上限，等待人工变更。",
            root_cause="Redis maxclients 接近上限",
            manual_action_required=True,
            approval_status="approved",
            markdown="# order-service AIOps 诊断报告",
        )
    )

    monkeypatch.setattr(aiops_service_module, "trace_service", trace_store)
    monkeypatch.setattr(aiops_service_module, "report_generator", report_store)

    events = [
        event
        async for event in service.resume_after_approval(
            session_id="missing-after-restart",
            incident_id=incident_id,
            approval=approval,
        )
    ]

    assert [event["type"] for event in events] == ["status", "report", "complete"]
    assert events[0]["execution_boundary"] == "agent_does_not_execute_production_change"
    assert "不会自动执行生产变更" in events[0]["message"]
    assert events[-1]["status"] == "approval_resumed"
    assert events[-1]["resume_source"] == "report_fallback"
    assert events[-1]["execution_boundary"] == "agent_does_not_execute_production_change"
    assert "未执行任何生产变更" in events[-1]["message"]
    assert events[-1]["structured_report"]["approval_status"] == "approved"
    assert "审批恢复记录" in events[-1]["structured_report"]["markdown"]
    assert report_store.get_report(incident_id).status == "approval_resumed"
    assert trace_store.list_events(incident_id=incident_id, event_type="diagnosis_resumed")
    assert service.get_session_snapshot("missing-after-restart").status == "approval_resumed"


@pytest.mark.asyncio
async def test_resume_after_approval_uses_persisted_session_snapshot_when_checkpoint_is_missing(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-resume-snapshot"
    trace_id = "trace-resume-snapshot"
    session_id = "session-resume-snapshot"
    database_path = tmp_path / "resume-snapshot.db"
    trace_store = TraceService(database_path)
    report_store = ReportGenerator(database_path)
    state_store = AIOpsSQLiteStore(database_path)
    service = AIOpsService()
    service.state_store = state_store
    approval = ApprovalRequest(
        incident_id=incident_id,
        action="人工调整 Redis maxclients",
        risk_level="high",
        reason="生产配置变更需要审批",
        status="approved",
        decided_by="pytest",
        decision_reason="approved from durable session snapshot",
        metadata={"trace_id": trace_id, "session_id": session_id},
    )
    snapshot = AIOpsSessionSnapshot.from_state(
        session_id=session_id,
        status="waiting_approval",
        node_name="replanner",
        state={
            "input": "诊断 order-service Redis timeout",
            "trace_id": trace_id,
            "incident": {
                "incident_id": incident_id,
                "title": "order-service Redis timeout",
                "service_name": "order-service",
                "severity": "P1",
                "environment": "prod",
                "symptom": "503 升高，Redis connection timeout",
            },
            "past_steps": [
                (
                    {"step_id": "step-redis", "tool_name": "query_redis_status"},
                    "Redis connected_clients 接近 maxclients",
                )
            ],
            "tool_call_records": [
                {
                    "trace_id": trace_id,
                    "incident_id": incident_id,
                    "step_id": "step-redis",
                    "tool_name": "query_redis_status",
                    "status": "success",
                    "data_source": "redis_info",
                    "output_summary": "connected_clients 接近 maxclients",
                }
            ],
            "gathered_evidence": [
                {
                    "evidence_id": "ev-redis",
                    "source_tool": "query_redis_status",
                    "step_id": "step-redis",
                    "summary": "Redis 连接数接近上限",
                    "fact": "connected_clients 接近 maxclients",
                    "data_source": "redis_info",
                    "confidence": 0.88,
                }
            ],
            "risk_assessment": {
                "policy": "approval_required",
                "risk_level": "high",
                "action": "人工调整 Redis maxclients",
            },
            "pending_approval": approval.model_dump(mode="json"),
        },
    )
    state_store.save_aiops_session_snapshot(snapshot)

    monkeypatch.setattr(aiops_service_module, "trace_service", trace_store)
    monkeypatch.setattr(aiops_service_module, "report_generator", report_store)

    events = [
        event
        async for event in service.resume_after_approval(
            session_id=session_id,
            incident_id=incident_id,
            approval=approval,
        )
    ]

    assert [event["type"] for event in events] == ["status", "report", "complete"]
    assert events[0]["resume_source"] == "session_snapshot"
    assert events[0]["execution_boundary"] == "agent_does_not_execute_production_change"
    assert events[-1]["status"] == "approval_resumed"
    assert events[-1]["resume_source"] == "session_snapshot"
    assert events[-1]["execution_boundary"] == "agent_does_not_execute_production_change"
    assert events[-1]["structured_report"]["approval_status"] == "approved"
    assert "Redis" in events[-1]["structured_report"]["markdown"]
    saved_snapshot = service.get_session_snapshot(session_id)
    assert saved_snapshot.status == "approval_resumed"
    assert saved_snapshot.pending_approval is None
    assert saved_snapshot.final_report_id == events[-1]["structured_report"]["report_id"]
    assert report_store.get_report(incident_id).status == "approval_resumed"
