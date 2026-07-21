"""Service tests for approved safe change workflows."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest

from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution, ManualExecutionResultRequest
from app.models.incident import utc_now
from app.models.report import DiagnosisReport
from app.services.approval_service import ApprovalService
from app.services.change_execution_service import ChangeExecutionService, ChangeExecutionStateError
from app.services.change_plan_builder import build_change_plan
from app.services.policies.approval_policy import RISK_POLICY_VERSION
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


def _tamper_approval_plan(
    approval_store: ApprovalService,
    approval_id: str,
    *,
    update_plan,
) -> None:
    with sqlite3.connect(approval_store.database_path) as connection:
        row = connection.execute(
            "SELECT payload FROM approval_requests WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["change_plan"] = update_plan(payload["change_plan"])
        connection.execute(
            "UPDATE approval_requests SET payload = ? WHERE approval_id = ?",
            (json.dumps(payload), approval_id),
        )


def test_sqlite_store_creates_change_execution_once_without_overwrite(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "safe-change-store.db")
    execution = ChangeExecution(
        change_execution_id="chgexec-stable",
        change_plan_id="plan-1",
        approval_id="approval-1",
        incident_id="inc-1",
        status="created",
    )

    first, first_created = store.create_change_execution_once(execution)
    second, second_created = store.create_change_execution_once(
        execution.model_copy(update={"status": "precheck_running"})
    )

    assert first_created is True
    assert first.change_execution_id == execution.change_execution_id
    assert second_created is False
    assert second.status == "created"


def test_sqlite_store_treats_approval_plan_scope_as_idempotency_key(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "safe-change-store.db")
    execution = ChangeExecution(
        change_execution_id="chgexec-first",
        change_plan_id="plan-1",
        approval_id="approval-1",
        incident_id="inc-1",
        status="created",
    )

    first, first_created = store.create_change_execution_once(execution)
    second, second_created = store.create_change_execution_once(
        execution.model_copy(
            update={
                "change_execution_id": "chgexec-retry-with-new-id",
                "status": "precheck_running",
            }
        )
    )

    assert first_created is True
    assert first.change_execution_id == "chgexec-first"
    assert second_created is False
    assert second.change_execution_id == "chgexec-first"
    assert second.status == "created"


def test_sqlite_store_rejects_legacy_duplicate_change_execution_scopes(
    tmp_path,
) -> None:
    database_path = tmp_path / "legacy-safe-change-store.db"
    first = ChangeExecution(
        change_execution_id="chgexec-first",
        change_plan_id="plan-1",
        approval_id="approval-1",
        incident_id="inc-1",
        status="created",
        created_at=utc_now() - timedelta(minutes=2),
    )
    duplicate = first.model_copy(
        update={
            "change_execution_id": "chgexec-existing-duplicate",
            "status": "precheck_running",
            "created_at": utc_now() - timedelta(minutes=1),
        }
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("""
            CREATE TABLE change_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_execution_id TEXT NOT NULL UNIQUE,
                change_plan_id TEXT NOT NULL,
                approval_id TEXT NOT NULL,
                incident_id TEXT NOT NULL,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """)
        for execution in (first, duplicate):
            connection.execute(
                """
                INSERT INTO change_executions (
                    change_execution_id, change_plan_id, approval_id, incident_id,
                    status, mode, created_at, updated_at, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution.change_execution_id,
                    execution.change_plan_id,
                    execution.approval_id,
                    execution.incident_id,
                    execution.status,
                    execution.mode,
                    execution.created_at.isoformat(),
                    execution.updated_at.isoformat(),
                    execution.model_dump_json(),
                ),
            )

    with pytest.raises(RuntimeError, match="duplicate business-scope groups"):
        AIOpsSQLiteStore(database_path)

    with sqlite3.connect(database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM change_executions").fetchone()[0]
    assert count == 2


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
    approval_step_id = "approved-remediation-step"
    plan = build_change_plan(
        incident_id=incident_id,
        action=action,
        risk_level=risk_level,
        tool_name="suggest_remediation",
        service_name="order-service",
        environment=environment,
        reason="生产变更需要审批",
        metadata={
            "trace_id": "trace-safe-change",
            "step_id": approval_step_id,
            "risk_policy_version": RISK_POLICY_VERSION,
            **(metadata or {}),
        },
    )
    request = approval_store.create_request(
        ApprovalRequest(
            incident_id=incident_id,
            action=action,
            risk_level=risk_level,
            reason="生产变更需要审批",
            step_id=approval_step_id,
            tool_name="suggest_remediation",
            risk_policy_version=RISK_POLICY_VERSION,
            change_plan=plan,
            metadata={
                "trace_id": "trace-safe-change",
                "input_args": dict(plan.metadata["approved_input_args"]),
                "change_plan": plan.model_dump(mode="json"),
            },
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
async def test_safe_change_dry_run_only_validates_without_resolving_incident(tmp_path) -> None:
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
    assert events[-1]["status"] == "dry_run_completed"
    assert events[-1]["change_execution"]["lifecycle_status"] == "change_validated"
    assert events[-1]["change_execution"]["stages"][2]["status"] == "skipped"
    execution = service.list_executions(incident_id=approval.incident_id)[0]
    assert execution.status == "dry_run_completed"
    assert execution.pre_check is not None
    assert execution.pre_check.status == "passed"
    assert execution.dry_run is not None
    assert execution.dry_run.status == "passed"
    assert trace_store.list_events(incident_id=approval.incident_id, event_type="change_precheck")
    assert trace_store.list_events(incident_id=approval.incident_id, event_type="change_dry_run")

    updated_report = report_store.get_report(approval.incident_id)
    assert updated_report is not None
    assert updated_report.status == "change_validated"
    assert updated_report.manual_action_required is False
    assert updated_report.change_executions[0]["status"] == "dry_run_completed"
    assert any("尚不能证明生产故障已经恢复" in item for item in updated_report.uncertainties)

    state = _state_store(service).get_incident_state(approval.incident_id)
    assert state is not None
    assert state.status == "change_validated"
    assert state.latest_approval_id == approval.approval_id
    assert state.manual_action_required is False

    report_store.save_report(old_report)
    preserved_state = _state_store(service).get_incident_state(approval.incident_id)
    assert preserved_state is not None
    assert preserved_state.status == "change_validated"
    assert preserved_state.latest_approval_id == approval.approval_id
    assert preserved_state.manual_action_required is False


@pytest.mark.asyncio
async def test_dry_run_can_resume_into_manual_record_waiting_state(tmp_path) -> None:
    service, approval_store, _, report_store = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    _save_approved_report(report_store, incident_id=approval.incident_id)

    first = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="dry_run_only",
        operator="pytest",
    )
    second = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="pytest",
    )

    assert first[-1]["status"] == "dry_run_completed"
    assert second[0]["stage"] == "waiting_manual_execution"
    assert second[-1]["status"] == "waiting_manual_execution"
    executions = service.list_executions(incident_id=approval.incident_id)
    assert len(executions) == 1
    assert executions[0].mode == "manual_record"
    assert executions[0].status == "waiting_manual_execution"

    updated_report = report_store.get_report(approval.incident_id)
    assert updated_report is not None
    assert updated_report.status == "waiting_manual_execution"
    assert updated_report.manual_action_required is True


@pytest.mark.asyncio
async def test_dry_run_can_resume_into_sandbox_validation(tmp_path) -> None:
    service, approval_store, _, report_store = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store, environment="staging")
    _save_approved_report(report_store, incident_id=approval.incident_id)

    first = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="dry_run_only",
        operator="pytest",
    )
    second = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="sandbox",
        operator="pytest",
    )

    assert first[-1]["status"] == "dry_run_completed"
    assert second[0]["stage"] == "sandbox_observed"
    assert second[0]["status"] == "passed"
    assert second[-1]["status"] == "sandbox_validated"
    executions = service.list_executions(incident_id=approval.incident_id)
    assert len(executions) == 1
    assert executions[0].mode == "sandbox"
    assert executions[0].status == "sandbox_validated"
    assert executions[0].observation is not None

    updated_report = report_store.get_report(approval.incident_id)
    assert updated_report is not None
    assert updated_report.status == "change_validated"
    assert updated_report.change_executions[-1]["status"] == "sandbox_validated"


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_mode", ["manual_record", "sandbox"])
async def test_stale_plan_blocks_resume_from_completed_dry_run(
    tmp_path,
    resume_mode: str,
) -> None:
    service, approval_store, trace_store, report_store = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store, environment="staging")
    _save_approved_report(report_store, incident_id=approval.incident_id)

    first = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="dry_run_only",
        operator="pytest",
    )
    stale_plan = plan.model_copy(
        update={
            "created_at": utc_now() - timedelta(hours=2),
            "expires_in_seconds": 60,
        }
    )
    _tamper_approval_plan(
        approval_store,
        approval.approval_id,
        update_plan=lambda payload: {
            **payload,
            "created_at": stale_plan.created_at.isoformat(),
            "expires_in_seconds": stale_plan.expires_in_seconds,
        },
    )

    second = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=stale_plan.change_plan_id,
        approval_id=approval.approval_id,
        mode=resume_mode,
        operator="pytest",
    )

    assert first[-1]["status"] == "dry_run_completed"
    assert second[0]["stage"] == "precheck_completed"
    assert second[0]["status"] == "failed"
    assert "过期窗口" in second[0]["message"]
    assert second[-1]["status"] == "precheck_failed"
    execution = service.list_executions(incident_id=approval.incident_id)[0]
    assert execution.status == "precheck_failed"
    assert execution.dry_run is not None
    assert execution.pre_check is not None
    assert "过期窗口" in execution.pre_check.reason
    assert trace_store.list_events(incident_id=approval.incident_id, event_type="change_precheck")

    updated_report = report_store.get_report(approval.incident_id)
    assert updated_report is not None
    assert updated_report.change_executions[-1]["status"] == "precheck_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["prod", "production", "prd", "线上", "生产"])
async def test_prod_sandbox_without_local_sandbox_escalates_with_clear_message(
    tmp_path,
    environment: str,
) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store, environment=environment)

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
async def test_sandbox_validation_does_not_resolve_incident(tmp_path) -> None:
    service, approval_store, _, report_store = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store, environment="staging")
    _save_approved_report(report_store, incident_id=approval.incident_id)

    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="sandbox",
        operator="pytest",
    )

    observation_event = events[-2]
    assert observation_event["stage"] == "sandbox_observed"
    assert observation_event["status"] == "passed"
    assert observation_event["change_execution"]["status"] == "sandbox_validated"
    assert observation_event["change_execution"]["lifecycle_status"] == "change_validated"
    assert events[-1]["status"] == "sandbox_validated"

    execution = service.list_executions(incident_id=approval.incident_id)[0]
    assert execution.status == "sandbox_validated"
    assert execution.observation is not None
    assert execution.observation.status == "passed"

    updated_report = report_store.get_report(approval.incident_id)
    assert updated_report is not None
    assert updated_report.status == "change_validated"
    assert updated_report.manual_action_required is False
    assert updated_report.change_executions[-1]["status"] == "sandbox_validated"
    assert any(
        "sandbox" in item and "尚不能证明生产故障已经恢复" in item
        for item in updated_report.uncertainties
    )

    state = _state_store(service).get_incident_state(approval.incident_id)
    assert state is not None
    assert state.status == "change_validated"
    assert state.manual_action_required is False


@pytest.mark.asyncio
async def test_high_risk_plan_without_rollback_stops_at_precheck(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    _tamper_approval_plan(
        approval_store,
        approval.approval_id,
        update_plan=lambda payload: {
            **payload,
            "rollback_steps": [],
            "rollback_plan": [],
        },
    )

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
    stale_created_at = utc_now() - timedelta(hours=2)
    _tamper_approval_plan(
        approval_store,
        approval.approval_id,
        update_plan=lambda payload: {
            **payload,
            "created_at": stale_created_at.isoformat(),
            "expires_in_seconds": 60,
        },
    )

    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
    )

    assert events[-1]["status"] == "precheck_failed"
    execution = service.list_executions(incident_id=approval.incident_id)[0]
    assert execution.pre_check is not None
    assert "过期窗口" in execution.pre_check.reason


@pytest.mark.asyncio
async def test_dry_run_failure_never_enters_manual_execution(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    plan = build_change_plan(
        incident_id="inc-dry-run-failure",
        action="人工调整 Redis maxclients",
        risk_level="high",
        tool_name="suggest_remediation",
        service_name="order-service",
        environment="prod",
        metadata={
            "trace_id": "trace-dry-run-failure",
            "step_id": "dry-run-failure-step",
            "risk_policy_version": RISK_POLICY_VERSION,
        },
    )
    blocked_step = plan.steps[0].model_copy(update={"can_dry_run": False})
    blocked_plan = plan.model_copy(update={"steps": [blocked_step]})
    request = approval_store.create_request(
        ApprovalRequest(
            incident_id=blocked_plan.incident_id,
            action=blocked_plan.action,
            risk_level=blocked_plan.risk_level,
            reason="production change requires approval",
            step_id="dry-run-failure-step",
            tool_name="suggest_remediation",
            risk_policy_version=RISK_POLICY_VERSION,
            change_plan=blocked_plan,
            metadata={
                "trace_id": "trace-dry-run-failure",
                "input_args": dict(blocked_plan.metadata["approved_input_args"]),
            },
        )
    )
    approval = approval_store.decide_request(
        request.approval_id,
        decision="approve",
        decided_by="approver",
    )
    assert approval.change_plan is not None

    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=approval.change_plan.change_plan_id,
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
            operator="change-operator",
            notes="人工已在变更平台执行并确认指标恢复",
            evidence={"change_ticket": "CHG-001"},
            observed_metrics={"service_5xx_rate": 0.0},
            step_results=[{"step_id": plan.steps[0].step_id, "status": "succeeded"}],
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
async def test_approver_cannot_record_manual_execution_result(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="change-operator",
    )
    execution_id = events[-1]["change_execution"]["change_execution_id"]

    with pytest.raises(ChangeExecutionStateError, match="other than the approver"):
        service.record_manual_result(
            execution_id,
            ManualExecutionResultRequest(
                status="succeeded",
                operator=str(approval.decided_by),
                notes="self-approved execution",
                evidence={"change_ticket": "CHG-SELF"},
                observed_metrics={"service_5xx_rate": 0.0},
            ),
        )

    persisted = service.get_execution(execution_id)
    assert persisted.status == "waiting_manual_execution"
    assert not persisted.manual_result


@pytest.mark.asyncio
async def test_manual_result_revalidates_exact_approved_plan(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="change-operator",
    )
    execution_id = events[-1]["change_execution"]["change_execution_id"]
    with sqlite3.connect(approval_store.database_path) as connection:
        row = connection.execute(
            "SELECT payload FROM approval_requests WHERE approval_id = ?",
            (approval.approval_id,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["change_plan"]["action"] = "expanded unapproved action"
        connection.execute(
            "UPDATE approval_requests SET payload = ? WHERE approval_id = ?",
            (json.dumps(payload), approval.approval_id),
        )

    with pytest.raises(ChangeExecutionStateError, match="no longer valid"):
        service.record_manual_result(
            execution_id,
            ManualExecutionResultRequest(
                status="succeeded",
                operator="separate-operator",
                notes="execution completed",
                evidence={"change_ticket": "CHG-DRIFT"},
                observed_metrics={"service_5xx_rate": 0.0},
            ),
        )

    assert service.get_execution(execution_id).status == "waiting_manual_execution"


@pytest.mark.asyncio
async def test_concurrent_manual_results_have_single_winner(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="change-operator",
    )
    execution_id = events[-1]["change_execution"]["change_execution_id"]
    barrier = threading.Barrier(2)

    def submit(operator: str):
        barrier.wait()
        try:
            return service.record_manual_result(
                execution_id,
                ManualExecutionResultRequest(
                    status="succeeded",
                    operator=operator,
                    notes=f"result from {operator}",
                    evidence={"change_ticket": f"CHG-{operator}"},
                    observed_metrics={"service_5xx_rate": 0.0},
                    step_results=[{"step_id": plan.steps[0].step_id, "status": "succeeded"}],
                ),
            )
        except ChangeExecutionStateError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ["operator-a", "operator-b"]))

    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, ChangeExecutionStateError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert service.get_execution(execution_id).status == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("manual_status", "execution_status"),
    [
        ("partial", "partial_success"),
        ("recovery_pending", "recovery_pending"),
        ("rolled_back", "rolled_back"),
        ("rollback_failed", "rollback_failed"),
    ],
)
async def test_manual_result_preserves_extended_recovery_states(
    tmp_path,
    manual_status,
    execution_status,
) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="change-operator",
    )
    execution_id = events[-1]["change_execution"]["change_execution_id"]

    request_kwargs = {
        "status": manual_status,
        "operator": "separate-operator",
        "notes": f"manual result {manual_status}",
        "evidence": {"change_ticket": "CHG-STATE"},
        "observed_metrics": {"service_5xx_rate": 0.1},
    }
    if manual_status == "rollback_failed":
        request_kwargs["evidence"] = {}
        request_kwargs["observed_metrics"] = {}
    if manual_status == "rolled_back":
        request_kwargs["step_results"] = [
            {"step_id": plan.steps[0].step_id, "status": "rolled_back"}
        ]

    updated = service.record_manual_result(
        execution_id,
        ManualExecutionResultRequest(**request_kwargs),
    )

    assert updated.status == execution_status


@pytest.mark.asyncio
async def test_partial_execution_can_transition_to_rolled_back(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="change-operator",
    )
    execution_id = events[-1]["change_execution"]["change_execution_id"]

    partial = service.record_manual_result(
        execution_id,
        ManualExecutionResultRequest(
            status="partial",
            operator="separate-operator",
            notes="one approved step completed",
            evidence={"change_ticket": "CHG-PARTIAL"},
            observed_metrics={"service_5xx_rate": 0.2},
        ),
    )
    rolled_back = service.record_manual_result(
        execution_id,
        ManualExecutionResultRequest(
            status="rolled_back",
            operator="separate-operator",
            notes="completed rollback for the changed scope",
            evidence={"change_ticket": "CHG-PARTIAL"},
            observed_metrics={"service_5xx_rate": 0.0},
            step_results=[{"step_id": plan.steps[0].step_id, "status": "rolled_back"}],
        ),
    )

    assert partial.status == "partial_success"
    assert rolled_back.status == "rolled_back"
    assert rolled_back.manual_result["history"][0]["status"] == "partial"


@pytest.mark.asyncio
async def test_recovery_pending_can_close_after_observation(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="change-operator",
    )
    execution_id = events[-1]["change_execution"]["change_execution_id"]

    service.record_manual_result(
        execution_id,
        ManualExecutionResultRequest(
            status="recovery_pending",
            operator="separate-operator",
            notes="waiting for the full observation window",
            evidence={"change_ticket": "CHG-RECOVERY"},
            observed_metrics={"service_5xx_rate": 0.05},
        ),
    )
    closed = service.record_manual_result(
        execution_id,
        ManualExecutionResultRequest(
            status="succeeded",
            operator="separate-operator",
            notes="observation window passed",
            evidence={"change_ticket": "CHG-RECOVERY"},
            observed_metrics={"service_5xx_rate": 0.0},
            step_results=[{"step_id": plan.steps[0].step_id, "status": "succeeded"}],
        ),
    )

    assert closed.status == "closed"
    assert closed.manual_result["history"][0]["status"] == "recovery_pending"


@pytest.mark.asyncio
async def test_multi_step_success_requires_every_approved_step_result(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    plan = build_change_plan(
        incident_id="inc-multi-step",
        action="multi-step manual change",
        risk_level="high",
        tool_name="suggest_remediation",
        service_name="order-service",
        environment="prod",
        metadata={
            "step_id": "multi-step-approved",
            "risk_policy_version": RISK_POLICY_VERSION,
        },
    )
    second_step = plan.steps[0].model_copy(update={"step_id": "second-approved-step"})
    expanded_plan = plan.model_copy(update={"steps": [*plan.steps, second_step]})
    pending = approval_store.create_request(
        ApprovalRequest(
            incident_id=expanded_plan.incident_id,
            action=expanded_plan.action,
            risk_level=expanded_plan.risk_level,
            step_id="multi-step-approved",
            tool_name="suggest_remediation",
            risk_policy_version=RISK_POLICY_VERSION,
            change_plan=expanded_plan,
            metadata={"input_args": dict(expanded_plan.metadata["approved_input_args"])},
        )
    )
    approval = approval_store.decide_request(
        pending.approval_id,
        decision="approve",
        decided_by="approver",
    )
    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=expanded_plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="change-operator",
    )
    execution_id = events[-1]["change_execution"]["change_execution_id"]

    with pytest.raises(ChangeExecutionStateError, match="every approved step"):
        service.record_manual_result(
            execution_id,
            ManualExecutionResultRequest(
                status="succeeded",
                operator="separate-operator",
                notes="only one step recorded",
                evidence={"change_ticket": "CHG-MULTI"},
                observed_metrics={"service_5xx_rate": 0.0},
                step_results=[
                    {
                        "step_id": expanded_plan.steps[0].step_id,
                        "status": "succeeded",
                    }
                ],
            ),
        )


@pytest.mark.asyncio
async def test_multi_step_partial_requires_complete_mixed_step_results(tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    plan = build_change_plan(
        incident_id="inc-multi-step-partial",
        action="multi-step manual change",
        risk_level="high",
        tool_name="suggest_remediation",
        service_name="order-service",
        environment="prod",
        metadata={
            "step_id": "multi-step-partial-approved",
            "risk_policy_version": RISK_POLICY_VERSION,
        },
    )
    first_step = plan.steps[0]
    first_rollback = plan.rollback_plan[0]
    second_rollback = first_rollback.model_copy(update={"step_id": "rollback-second"})
    second_step = first_step.model_copy(
        update={
            "step_id": "execute-second",
            "rollback_step_id": second_rollback.step_id,
        }
    )
    expanded_plan = plan.model_copy(
        update={
            "steps": [first_step, second_step],
            "rollback_plan": [first_rollback, second_rollback],
        }
    )
    pending = approval_store.create_request(
        ApprovalRequest(
            incident_id=expanded_plan.incident_id,
            action=expanded_plan.action,
            risk_level=expanded_plan.risk_level,
            step_id="multi-step-partial-approved",
            tool_name="suggest_remediation",
            risk_policy_version=RISK_POLICY_VERSION,
            change_plan=expanded_plan,
            metadata={"input_args": dict(expanded_plan.metadata["approved_input_args"])},
        )
    )
    approval = approval_store.decide_request(
        pending.approval_id,
        decision="approve",
        decided_by="approver",
    )
    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=expanded_plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="change-operator",
    )
    execution_id = events[-1]["change_execution"]["change_execution_id"]

    with pytest.raises(ChangeExecutionStateError, match="multi-step partial"):
        service.record_manual_result(
            execution_id,
            ManualExecutionResultRequest(
                status="partial",
                operator="separate-operator",
                notes="only one step was described",
                evidence={"change_ticket": "CHG-MULTI-PARTIAL"},
                observed_metrics={"service_5xx_rate": 0.2},
                step_results=[{"step_id": first_step.step_id, "status": "succeeded"}],
            ),
        )

    updated = service.record_manual_result(
        execution_id,
        ManualExecutionResultRequest(
            status="partial",
            operator="separate-operator",
            notes="first step succeeded and second step was stopped",
            evidence={"change_ticket": "CHG-MULTI-PARTIAL"},
            observed_metrics={"service_5xx_rate": 0.2},
            step_results=[
                {"step_id": first_step.step_id, "status": "succeeded"},
                {"step_id": second_step.step_id, "status": "skipped"},
            ],
        ),
    )

    assert updated.status == "partial_success"


@pytest.mark.asyncio
async def test_manual_result_survives_projection_failures(monkeypatch, tmp_path) -> None:
    service, approval_store, trace_store, report_store = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="change-operator",
    )
    execution_id = events[-1]["change_execution"]["change_execution_id"]

    monkeypatch.setattr(
        service._store,
        "save_incident_state",
        lambda state: (_ for _ in ()).throw(RuntimeError("state unavailable")),
    )
    monkeypatch.setattr(
        report_store,
        "mark_change_execution_updated",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("report unavailable")),
    )
    monkeypatch.setattr(
        trace_store,
        "record_change_event",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("trace unavailable")),
    )

    updated = service.record_manual_result(
        execution_id,
        ManualExecutionResultRequest(
            status="succeeded",
            operator="separate-operator",
            notes="external change completed",
            evidence={"change_ticket": "CHG-PROJECTION"},
            observed_metrics={"service_5xx_rate": 0.0},
            step_results=[{"step_id": plan.steps[0].step_id, "status": "succeeded"}],
        ),
    )

    assert updated.status == "closed"
    assert service.get_execution(execution_id).status == "closed"


@pytest.mark.asyncio
async def test_manual_result_projection_failures_are_marked_and_repaired(
    monkeypatch,
    tmp_path,
) -> None:
    service, approval_store, trace_store, report_store = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="change-operator",
    )
    execution_id = events[-1]["change_execution"]["change_execution_id"]
    original_save_state = service._store.save_incident_state
    original_report_update = report_store.mark_change_execution_updated
    original_trace_update = trace_store.record_change_event
    monkeypatch.setattr(
        service._store,
        "save_incident_state",
        lambda state: (_ for _ in ()).throw(RuntimeError("state unavailable")),
    )
    monkeypatch.setattr(
        report_store,
        "mark_change_execution_updated",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("report unavailable")),
    )
    monkeypatch.setattr(
        trace_store,
        "record_change_event",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("trace unavailable")),
    )

    updated = service.record_manual_result(
        execution_id,
        ManualExecutionResultRequest(
            status="succeeded",
            operator="separate-operator",
            notes="external change completed",
            evidence={"change_ticket": "CHG-REPAIR"},
            observed_metrics={"service_5xx_rate": 0.0},
            step_results=[{"step_id": plan.steps[0].step_id, "status": "succeeded"}],
        ),
    )

    assert updated.projection_pending == ["incident_state", "report", "trace"]
    monkeypatch.setattr(service._store, "save_incident_state", original_save_state)
    monkeypatch.setattr(report_store, "mark_change_execution_updated", original_report_update)
    monkeypatch.setattr(trace_store, "record_change_event", original_trace_update)

    repaired = service.get_execution(execution_id)
    assert repaired.projection_pending == []
    state = service._store.get_incident_state(approval.incident_id)
    assert state is not None
    assert state.status == "resolved"


@pytest.mark.asyncio
async def test_manual_result_persists_projection_outbox_before_sync(monkeypatch, tmp_path) -> None:
    service, approval_store, _, _ = _build_runtime(tmp_path)
    approval, plan = _approved_request(approval_store)
    events = await _collect_events(
        service,
        incident_id=approval.incident_id,
        change_plan_id=plan.change_plan_id,
        approval_id=approval.approval_id,
        mode="manual_record",
        operator="change-operator",
    )
    execution_id = events[-1]["change_execution"]["change_execution_id"]
    monkeypatch.setattr(
        service,
        "_sync_committed_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("process stopped")),
    )

    with pytest.raises(RuntimeError, match="process stopped"):
        service.record_manual_result(
            execution_id,
            ManualExecutionResultRequest(
                status="succeeded",
                operator="separate-operator",
                notes="external change completed",
                evidence={"change_ticket": "CHG-OUTBOX"},
                observed_metrics={"service_5xx_rate": 0.0},
                step_results=[{"step_id": plan.steps[0].step_id, "status": "succeeded"}],
            ),
        )

    stored = service._store.get_change_execution(execution_id)
    assert stored is not None
    assert stored.status == "closed"
    assert stored.projection_pending == ["incident_state", "report", "trace"]


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
