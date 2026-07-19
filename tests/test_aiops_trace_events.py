"""Tests for trace events emitted by AIOps workflow components."""

import asyncio
import importlib
from typing import Any

import pytest

from app.agent.aiops import create_initial_aiops_state
from app.config import config
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.approval import ApprovalRequest
from app.models.plan import PlanStep
from app.models.report import DiagnosisReport
from app.services.aiops_service import AIOpsResumeConflictError, AIOpsService
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
    original_registry_factory = executor_module.create_default_tool_registry

    def registry_without_redis(*args, **kwargs):
        registry = original_registry_factory(*args, **kwargs)
        original_get = registry.get
        registry.get = lambda name: None if name == "query_redis_status" else original_get(name)
        return registry

    monkeypatch.setattr(
        executor_module,
        "create_default_tool_registry",
        registry_without_redis,
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
    assert events[0].metadata["data_source"] == "failed"


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
    service.state_store.save_aiops_session_snapshot(
        AIOpsSessionSnapshot.from_state(
            session_id="missing-after-restart",
            status="waiting_approval",
            state={
                "trace_id": trace_id,
                "incident": {"incident_id": incident_id},
                "pending_approval": approval.model_dump(mode="json"),
            },
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

    assert [event["type"] for event in events] == [
        "progress",
        "status",
        "progress",
        "report",
        "progress",
        "complete",
    ]
    business_events = [event for event in events if event["type"] != "progress"]
    assert [event["type"] for event in business_events] == ["status", "report", "complete"]
    assert events[0]["progress_cursor"]
    assert business_events[0]["execution_boundary"] == "agent_does_not_execute_production_change"
    assert "不会自动执行生产变更" in business_events[0]["message"]
    assert events[-1]["status"] == "approval_resumed"
    assert events[-1]["resume_source"] == "session_snapshot"
    assert events[-1]["execution_boundary"] == "agent_does_not_execute_production_change"
    assert "未执行任何生产变更" in events[-1]["message"]
    assert events[-1]["structured_report"]["approval_status"] == "approved"
    assert "AIOps 诊断报告" in events[-1]["structured_report"]["markdown"]
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

    assert [event["type"] for event in events] == [
        "progress",
        "status",
        "progress",
        "report",
        "progress",
        "complete",
    ]
    business_events = [event for event in events if event["type"] != "progress"]
    assert [event["type"] for event in business_events] == ["status", "report", "complete"]
    assert events[0]["progress_cursor"]
    assert business_events[0]["resume_source"] == "session_snapshot"
    assert business_events[0]["execution_boundary"] == "agent_does_not_execute_production_change"
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


def test_diagnosis_session_claim_rejects_existing_snapshot(tmp_path) -> None:
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(tmp_path / "session-claim.db")
    service.state_store.save_aiops_session_snapshot(
        AIOpsSessionSnapshot.from_state(
            session_id="session-existing",
            status="completed",
            state={
                "trace_id": "trace-existing",
                "incident": {"incident_id": "inc-existing"},
            },
        )
    )

    service._claim_diagnosis_session("session-existing")
    service._release_diagnosis_session("session-existing")


def test_atomic_session_snapshot_create_rejects_duplicate_identity(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "session-create.db")
    first = AIOpsSessionSnapshot.from_state(
        session_id="session-atomic",
        state={"trace_id": "trace-first", "incident": {"incident_id": "inc-first"}},
    )
    second = AIOpsSessionSnapshot.from_state(
        session_id="session-atomic",
        state={"trace_id": "trace-second", "incident": {"incident_id": "inc-second"}},
    )

    assert store.create_aiops_session_snapshot(first) is True
    assert store.create_aiops_session_snapshot(second) is False
    saved = store.get_aiops_session_snapshot("session-atomic")
    assert saved is not None
    assert saved.trace_id == "trace-first"
    assert saved.incident_id == "inc-first"


def test_resolve_resume_session_id_scans_all_snapshot_pages(tmp_path) -> None:
    incident_id = "inc-resume-paged"
    approval = ApprovalRequest(
        approval_id="apr-resume-paged",
        incident_id=incident_id,
        action="manual mitigation",
        risk_level="high",
        status="approved",
        metadata={"trace_id": "trace-target", "session_id": "session-target"},
    )
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(tmp_path / "resume-paged.db")

    for index in range(101):
        state = {
            "trace_id": f"trace-other-{index}",
            "incident": {"incident_id": incident_id},
            "pending_approval": {"approval_id": f"apr-other-{index}"},
        }
        service.state_store.save_aiops_session_snapshot(
            AIOpsSessionSnapshot.from_state(
                session_id=f"session-other-{index}",
                status="waiting_approval",
                state=state,
            )
        )

    service.state_store.save_aiops_session_snapshot(
        AIOpsSessionSnapshot.from_state(
            session_id="session-target",
            status="waiting_approval",
            state={
                "trace_id": "trace-target",
                "incident": {"incident_id": incident_id},
                "pending_approval": approval.model_dump(mode="json"),
            },
        )
    )

    assert (
        service.resolve_resume_session_id(
            incident_id=incident_id,
            approval=approval,
        )
        == "session-target"
    )


def test_reconcile_incomplete_runs_scans_all_snapshot_pages(tmp_path) -> None:
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(tmp_path / "reconcile-paged.db")

    for index in range(101):
        service.state_store.save_aiops_session_snapshot(
            AIOpsSessionSnapshot.from_state(
                session_id=f"session-terminal-{index}",
                status="completed",
                state={
                    "trace_id": f"trace-terminal-{index}",
                    "incident": {"incident_id": f"inc-terminal-{index}"},
                },
            )
        )
    service.state_store.save_aiops_session_snapshot(
        AIOpsSessionSnapshot.from_state(
            session_id="session-running-after-page",
            status="running",
            state={
                "trace_id": "trace-running-after-page",
                "incident": {"incident_id": "inc-running-after-page"},
            },
        )
    )

    assert service.reconcile_incomplete_runs() == 1
    saved = service.get_session_snapshot("session-running-after-page")
    assert saved is not None
    assert saved.status == "failed"


def test_resolve_resume_session_id_requires_matching_paused_snapshot(tmp_path) -> None:
    incident_id = "inc-resume-identity"
    approval = ApprovalRequest(
        incident_id=incident_id,
        action="manual mitigation",
        risk_level="high",
        status="approved",
        metadata={"trace_id": "trace-old"},
    )
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(tmp_path / "resume-identity.db")
    service.state_store.save_aiops_session_snapshot(
        AIOpsSessionSnapshot.from_state(
            session_id="session-newer",
            status="waiting_approval",
            state={
                "trace_id": "trace-newer",
                "incident": {"incident_id": incident_id},
                "pending_approval": {"approval_id": "apr-other"},
            },
        )
    )

    with pytest.raises(LookupError, match="No durable session snapshot"):
        service.resolve_resume_session_id(incident_id=incident_id, approval=approval)


@pytest.mark.asyncio
async def test_resume_ignores_process_local_checkpoint_when_durable_snapshot_exists(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-durable-authority"
    session_id = "session-durable-authority"
    trace_id = "trace-durable-authority"
    database_path = tmp_path / "durable-authority.db"
    trace_store = TraceService(database_path)
    report_store = ReportGenerator(database_path)
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(database_path)
    approval = ApprovalRequest(
        incident_id=incident_id,
        action="manual mitigation",
        risk_level="high",
        status="approved",
        metadata={"trace_id": trace_id, "session_id": session_id},
    )
    service.state_store.save_aiops_session_snapshot(
        AIOpsSessionSnapshot.from_state(
            session_id=session_id,
            status="waiting_approval",
            state={
                "trace_id": trace_id,
                "incident": {
                    "incident_id": incident_id,
                    "service_name": "order-service",
                },
                "pending_approval": approval.model_dump(mode="json"),
                "final_diagnosis": "Durable Redis saturation diagnosis",
            },
        )
    )

    def fail_if_checkpoint_is_read(_session_id: str) -> dict:
        raise AssertionError("process-local checkpoint must not participate in recovery")

    monkeypatch.setattr(service, "get_runtime_checkpoint_values", fail_if_checkpoint_is_read)
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

    assert events[-1]["type"] == "complete"
    assert events[-1]["resume_source"] == "session_snapshot"
    assert events[-1]["structured_report"]["incident_id"] == incident_id
    assert service.get_session_snapshot(session_id).status == "approval_resumed"


def test_report_fallback_rejects_unrelated_approval() -> None:
    report = DiagnosisReport(
        incident_id="inc-report-identity",
        trace_id="trace-report-identity",
        title="Diagnosis report",
        service_name="order-service",
        severity="P1",
        environment="prod",
        status="approval_approved",
        summary="Waiting for approved mitigation",
        root_cause="Redis saturation",
        approval_decision={"approval_id": "apr-report"},
        markdown="# Diagnosis report",
    )
    approval = ApprovalRequest(
        approval_id="apr-other",
        incident_id=report.incident_id,
        action="manual mitigation",
        risk_level="high",
        status="approved",
        metadata={"trace_id": report.trace_id},
    )

    with pytest.raises(ValueError, match="persisted diagnosis report"):
        AIOpsService._validate_resume_report(report, approval)


@pytest.mark.asyncio
async def test_duplicate_resume_claim_is_rejected_until_first_finishes(monkeypatch) -> None:
    service = AIOpsService()
    approval = ApprovalRequest(
        incident_id="inc-resume-concurrent",
        action="manual mitigation",
        risk_level="high",
        status="approved",
    )
    release = asyncio.Event()
    started = asyncio.Event()

    async def blocking_resume(**_kwargs):
        started.set()
        yield {"type": "status"}
        await release.wait()
        yield {"type": "complete"}

    monkeypatch.setattr(service, "_resume_after_approval_impl", blocking_resume)

    first_stream = service.resume_after_approval(
        session_id="session-resume-concurrent",
        incident_id=approval.incident_id,
        approval=approval,
    )
    assert (await anext(first_stream))["type"] == "status"
    await started.wait()

    second_stream = service.resume_after_approval(
        session_id="session-resume-concurrent",
        incident_id=approval.incident_id,
        approval=approval,
    )
    with pytest.raises(AIOpsResumeConflictError, match="already in progress"):
        await anext(second_stream)

    release.set()
    assert (await anext(first_stream))["type"] == "complete"
    with pytest.raises(StopAsyncIteration):
        await anext(first_stream)


@pytest.mark.asyncio
async def test_duplicate_resume_is_rejected_after_first_completion(monkeypatch, tmp_path) -> None:
    incident_id = "inc-resume-once"
    trace_id = "trace-resume-once"
    session_id = "session-resume-once"
    database_path = tmp_path / "resume-once.db"
    trace_store = TraceService(database_path)
    report_store = ReportGenerator(database_path)
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(database_path)
    approval = ApprovalRequest(
        incident_id=incident_id,
        action="manual mitigation",
        risk_level="high",
        status="approved",
        metadata={"trace_id": trace_id, "session_id": session_id},
    )
    service.state_store.save_aiops_session_snapshot(
        AIOpsSessionSnapshot.from_state(
            session_id=session_id,
            status="waiting_approval",
            state={
                "trace_id": trace_id,
                "incident": {
                    "incident_id": incident_id,
                    "service_name": "order-service",
                },
                "pending_approval": approval.model_dump(mode="json"),
            },
        )
    )
    monkeypatch.setattr(aiops_service_module, "trace_service", trace_store)
    monkeypatch.setattr(aiops_service_module, "report_generator", report_store)

    first_events = [
        event
        async for event in service.resume_after_approval(
            session_id=session_id,
            incident_id=incident_id,
            approval=approval,
        )
    ]
    assert first_events[-1]["type"] == "complete"

    with pytest.raises(ValueError, match="not waiting"):
        service.resolve_resume_session_id(
            incident_id=incident_id,
            approval=approval,
            requested_session_id=session_id,
        )


@pytest.mark.asyncio
async def test_resume_persistence_failure_does_not_emit_complete(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-resume-persistence-failed"
    trace_id = "trace-resume-persistence-failed"
    session_id = "session-resume-persistence-failed"
    database_path = tmp_path / "resume-persistence-failed.db"
    trace_store = TraceService(database_path)
    report_store = ReportGenerator(database_path)
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(database_path)
    approval = ApprovalRequest(
        incident_id=incident_id,
        action="manual mitigation",
        risk_level="high",
        status="approved",
        metadata={"trace_id": trace_id, "session_id": session_id},
    )
    service.state_store.save_aiops_session_snapshot(
        AIOpsSessionSnapshot.from_state(
            session_id=session_id,
            status="waiting_approval",
            state={
                "trace_id": trace_id,
                "incident": {
                    "incident_id": incident_id,
                    "service_name": "order-service",
                },
                "pending_approval": approval.model_dump(mode="json"),
            },
        )
    )
    original_save = service._save_session_snapshot

    def fail_terminal_save(**kwargs):
        if kwargs.get("required"):
            raise RuntimeError("snapshot unavailable")
        return original_save(**kwargs)

    monkeypatch.setattr(aiops_service_module, "trace_service", trace_store)
    monkeypatch.setattr(aiops_service_module, "report_generator", report_store)
    monkeypatch.setattr(service, "_save_session_snapshot", fail_terminal_save)

    events = []
    with pytest.raises(RuntimeError, match="snapshot unavailable"):
        async for event in service.resume_after_approval(
            session_id=session_id,
            incident_id=incident_id,
            approval=approval,
        ):
            events.append(event)

    assert not any(event["type"] == "complete" for event in events)
    snapshot = service.get_session_snapshot(session_id)
    assert snapshot is not None
    assert snapshot.status == "failed"
    assert snapshot.progress["status"] == "failed"


@pytest.mark.asyncio
async def test_failed_resume_retry_uses_distinct_progress_cursors(monkeypatch, tmp_path) -> None:
    incident_id = "inc-resume-retry-cursor"
    trace_id = "trace-resume-retry-cursor"
    session_id = "session-resume-retry-cursor"
    database_path = tmp_path / "resume-retry-cursor.db"
    trace_store = TraceService(database_path)
    report_store = ReportGenerator(database_path)
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(database_path)
    approval = ApprovalRequest(
        incident_id=incident_id,
        action="manual mitigation",
        risk_level="high",
        status="approved",
        metadata={"trace_id": trace_id, "session_id": session_id},
    )
    service.state_store.save_aiops_session_snapshot(
        AIOpsSessionSnapshot.from_state(
            session_id=session_id,
            status="failed",
            state={
                "trace_id": trace_id,
                "incident": {
                    "incident_id": incident_id,
                    "service_name": "order-service",
                },
                "pending_approval": approval.model_dump(mode="json"),
                "resume_approval_id": approval.approval_id,
                "resume_status": "failed",
                "resume_attempt": 1,
            },
        )
    )
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

    cursors = [event["progress_cursor"] for event in events if event.get("progress_cursor")]
    assert cursors
    assert all(":resume-02-" in cursor for cursor in cursors)
    assert len(set(cursors)) == 3
    saved = service.get_session_snapshot(session_id)
    assert saved is not None
    assert saved.resume_attempt == 2
    assert saved.resume_approval_id == approval.approval_id
    assert saved.resume_status == "completed"
