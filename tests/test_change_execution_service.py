"""Service tests for approved safe change workflows."""

from datetime import timedelta
from typing import Any

import pytest

from app.models.approval import ApprovalRequest
from app.models.change_execution import ManualExecutionResultRequest
from app.models.incident import utc_now
from app.models.report import DiagnosisReport
from app.services.approval_service import ApprovalService
from app.services.change_execution_service import ChangeExecutionService, ChangeExecutionStateError
from app.services.change_plan_builder import build_change_plan
from app.services.report_generator import ReportGenerator
from app.services.sqlite_store import AIOpsSQLiteStore
from app.services.trace_service import TraceService


def _build_runtime(tmp_path):
    database_path = tmp_path / "safe-change.db"
    trace_store = TraceService(database_path)
    report_store = ReportGenerator(database_path)
    approval_store = ApprovalService(database_path, sync_report_status=False)
    service = ChangeExecutionService(
        database_path,
        approval_repository=approval_store,
        trace_repository=trace_store,
        report_repository=report_store,
    )
    return service, approval_store, trace_store, report_store


def _state_store(service: ChangeExecutionService) -> AIOpsSQLiteStore:
    return AIOpsSQLiteStore(service.database_path)


def _save_approved_report(report_store: ReportGenerator, *, incident_id: str) -> DiagnosisReport:
    report = DiagnosisReport(
        incident_id=incident_id,
        trace_id="trace-safe-change",
        title="order-service AIOps 诊断报告",
        service_name="order-service",
        severity="P1",
        environment="prod",
        status="approval_approved",
        summary="审批已通过，等待进入安全变更流程。",
        root_cause="Redis maxclients 接近上限",
        approval_status="approved",
        approval_decision={"approval_id": "apr-existing", "status": "approved"},
        manual_action_required=True,
        markdown="# order-service AIOps 诊断报告",
    )
    return report_store.save_report(report)


def _approved_request(
    approval_store: ApprovalService,
    *,
    incident_id: str = "inc-redis",
    risk_level: str = "high",
    action: str = "人工调整 Redis maxclients",
    environment: str = "prod",
    metadata: dict[str, Any] | None = None,
):
    plan = build_change_plan(
        incident_id=incident_id,
        action=action,
        risk_level=risk_level,
        tool_name="suggest_remediation",
        service_name="order-service",
        environment=environment,
        reason="生产变更需要审批",
        metadata={"trace_id": "trace-safe-change", **(metadata or {})},
    )
    request = approval_store.create_request(
        ApprovalRequest(
            incident_id=incident_id,
            action=action,
            risk_level=risk_level,
            reason="生产变更需要审批",
            change_plan=plan,
            metadata={"trace_id": "trace-safe-change", "change_plan": plan.model_dump(mode="json")},
        )
    )
    approved = approval_store.decide_request(
        approval_id=request.approval_id,
        decision="approve",
        decided_by="pytest",
        reason="approved for safe change workflow",
    )
    assert approved.change_plan is not None
    return approved, approved.change_plan


async def _collect_events(service: ChangeExecutionService, **kwargs: Any) -> list[dict[str, Any]]:
    return [event async for event in service.start_after_approval(**kwargs)]


@pytest.mark.asyncio
async def test_safe_change_requires_approved_request(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    plan = build_change_plan(
        incident_id="inc-pending",
        action="重启服务",
        risk_level="high",
        service_name="order-service",
        environment="prod",
    )
    pending = approval_store.create_request(
        ApprovalRequest(
            incident_id="inc-pending",
            action=plan.action,
            risk_level="high",
            change_plan=plan,
            metadata={"trace_id": "trace-pending"},
        )
    )

    with pytest.raises(ChangeExecutionStateError, match="expected approved"):
        await _collect_events(
            service,
            incident_id="inc-pending",
            change_plan_id=plan.change_plan_id,
            approval_id=pending.approval_id,
        )


@pytest.mark.asyncio
async def test_safe_change_dry_run_only_closes_without_production_execution(tmp_path) -> None:
    service, approval_store, trace_store, report_store = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    old_report = _save_approved_report(report_store, incident_id=approval.incident_id)

    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="dry_run_only",
        operator="pytest",
    )

    assert [event["type"] for event in events] == [
        "change_precheck",
        "change_precheck",
        "change_dry_run",
        "change_dry_run",
        "change_report",
        "complete",
    ]
    assert events[-1]["status"] == "closed"
    assert events[-1]["change_execution"]["lifecycle_status"] == "resolved"
    assert events[-1]["change_execution"]["stages"][2]["status"] == "skipped"
    execution = service.list_executions(incident_id=approval.incident_id)[0]
    assert execution.status == "closed"
    assert execution.pre_check is not None
    assert execution.pre_check.status == "passed"
    assert execution.dry_run is not None
    assert execution.dry_run.status == "passed"
    assert trace_store.list_events(incident_id=approval.incident_id, event_type="change_precheck")
    assert trace_store.list_events(incident_id=approval.incident_id, event_type="change_dry_run")

    updated_report = report_store.get_report(approval.incident_id)
    assert updated_report is not None
    assert updated_report.status == "resolved"
    assert updated_report.manual_action_required is False
    assert updated_report.change_executions[0]["status"] == "closed"

    state = _state_store(service).get_incident_state(approval.incident_id)
    assert state is not None
    assert state.status == "resolved"
    assert state.latest_approval_id == approval.approval_id
    assert state.manual_action_required is False

    report_store.save_report(old_report)
    preserved_state = _state_store(service).get_incident_state(approval.incident_id)
    assert preserved_state is not None
    assert preserved_state.status == "resolved"
    assert preserved_state.latest_approval_id == approval.approval_id
    assert preserved_state.manual_action_required is False


@pytest.mark.asyncio
async def test_prod_sandbox_without_local_sandbox_escalates_with_clear_message(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)

    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="sandbox",
        operator="pytest",
    )

    observation_event = events[-2]
    assert observation_event["stage"] == "sandbox_escalated"
    assert observation_event["status"] == "escalated"
    assert "转人工接管" in observation_event["message"]
    assert "沙箱执行和观察已完成" not in observation_event["message"]
    assert events[-1]["status"] == "escalated"
    assert "未执行生产变更" in events[-1]["message"]


@pytest.mark.asyncio
async def test_high_risk_plan_without_rollback_stops_at_precheck(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    plan = plan.model_copy(update={"rollback_steps": [], "rollback_plan": []})
    approval = approval.model_copy(update={"change_plan": plan})
    approval_store.create_request(approval)

    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="dry_run_only",
    )

    assert events[-1]["status"] == "precheck_failed"
    execution = service.list_executions(incident_id=approval.incident_id)[0]
    assert execution.pre_check is not None
    assert "缺少 rollback plan" in execution.pre_check.reason
    assert execution.dry_run is None


@pytest.mark.asyncio
async def test_stale_plan_stops_at_precheck(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    stale_plan = plan.model_copy(
        update={
            "created_at": utc_now() - timedelta(hours=2),
            "expires_in_seconds": 60,
        }
    )
    approval = approval.model_copy(update={"change_plan": stale_plan})
    approval_store.create_request(approval)

    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=stale_plan.change_plan_id,
        approval_id=approval.approval_id,
    )

    assert events[-1]["status"] == "precheck_failed"
    execution = service.list_executions(incident_id=approval.incident_id)[0]
    assert execution.pre_check is not None
    assert "过期窗口" in execution.pre_check.reason


@pytest.mark.asyncio
async def test_dry_run_failure_never_enters_manual_execution(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    blocked_step = plan.steps[0].model_copy(update={"can_dry_run": False})
    blocked_plan = plan.model_copy(update={"steps": [blocked_step]})
    approval = approval.model_copy(update={"change_plan": blocked_plan})
    approval_store.create_request(approval)

    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=blocked_plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
    )

    assert events[-1]["status"] == "dry_run_failed"
    assert "change_execution" not in [event["type"] for event in events]
    execution = service.list_executions(incident_id=approval.incident_id)[0]
    assert execution.status == "dry_run_failed"
    assert execution.dry_run is not None
    assert execution.dry_run.blocked_steps == [blocked_step.step_id]


@pytest.mark.asyncio
async def test_manual_record_waits_then_closes_after_operator_result(tmp_path) -> None:
    service, approval_store, trace_store, report_store = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    _save_approved_report(report_store, incident_id=approval.incident_id)

    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="pytest",
    )

    assert events[-1]["status"] == "waiting_manual_execution"
    execution = service.list_executions(incident_id=approval.incident_id)[0]
    waiting_report = report_store.get_report(approval.incident_id)
    assert waiting_report is not None
    assert waiting_report.status == "waiting_manual_execution"
    assert waiting_report.manual_action_required is True
    assert waiting_report.change_executions[-1]["stages"][2]["status"] == (
        "waiting_manual_execution"
    )
    waiting_state = _state_store(service).get_incident_state(approval.incident_id)
    assert waiting_state is not None
    assert waiting_state.status == "waiting_manual_execution"
    assert waiting_state.latest_approval_id == approval.approval_id
    assert waiting_state.manual_action_required is True

    updated = service.record_manual_result(
        execution.change_execution_id,
        ManualExecutionResultRequest(
            status="succeeded",
            operator="pytest",
            notes="人工已在变更平台执行并确认指标恢复",
            observed_metrics={"service_5xx_rate": 0.0},
        ),
    )

    assert updated.status == "closed"
    assert updated.observation is not None
    assert updated.observation.status == "passed"
    assert trace_store.list_events(
        incident_id=approval.incident_id, event_type="change_observation"
    )
    closed_report = report_store.get_report(approval.incident_id)
    assert closed_report is not None
    assert closed_report.status == "resolved"
    assert closed_report.manual_action_required is False
    closed_state = _state_store(service).get_incident_state(approval.incident_id)
    assert closed_state is not None
    assert closed_state.status == "resolved"
    assert closed_state.latest_approval_id == approval.approval_id
    assert closed_state.manual_action_required is False


@pytest.mark.asyncio
async def test_same_approval_and_plan_resume_is_idempotent(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)

    first = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
    )
    second = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
    )

    executions = service.list_executions(incident_id=approval.incident_id)
    assert len(executions) == 1
    assert first[-1]["change_execution"]["change_execution_id"] == executions[0].change_execution_id
    assert second[0]["stage"] == "change_execution_existing"
    assert (
        second[-1]["change_execution"]["change_execution_id"] == executions[0].change_execution_id
    )
