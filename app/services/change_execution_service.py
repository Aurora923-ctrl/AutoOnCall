"""Safe change workflow orchestration after human approval."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from hashlib import sha256
from pathlib import Path
from typing import Any

from loguru import logger

from app.models.approval import ApprovalRequest
from app.models.change_execution import (
    ChangeExecution,
    ChangeExecutionMode,
    CheckStatus,
    DryRunResult,
    ManualExecutionResultRequest,
    ObservationResult,
    PreCheckResult,
)
from app.models.change_plan import ChangePlan
from app.models.incident import utc_now
from app.services.aiops_store import create_aiops_store
from app.services.approval_service import ApprovalNotFoundError, ApprovalService, approval_service
from app.services.change_execution_checks import (
    build_dry_run_result,
    build_pre_check_result,
    status_after_dry_run,
)
from app.services.change_execution_read_models import build_change_execution_read_model
from app.services.incident_state_builder import build_incident_state_from_change_execution
from app.services.report_generator import ReportGenerator, report_generator
from app.services.sqlite_store import resolve_sqlite_path
from app.services.trace_service import TraceService, trace_service


class ChangeExecutionNotFoundError(KeyError):
    """Raised when a safe change execution cannot be found."""


class ChangeExecutionStateError(ValueError):
    """Raised when a safe change workflow cannot transition state."""


class ChangeExecutionService:
    """Run the approved safe-change workflow without production write access."""

    def __init__(
        self,
        storage_path: str | Path | None = None,
        *,
        approval_repository: ApprovalService | None = None,
        trace_repository: TraceService | None = None,
        report_repository: ReportGenerator | None = None,
    ):
        raw_storage_path = Path(storage_path) if storage_path is not None else None
        self.database_path = resolve_sqlite_path(raw_storage_path)
        self._store = create_aiops_store(raw_storage_path)
        self.storage_path = getattr(self._store, "storage_path", self.database_path)
        self._approval_service = approval_repository or (
            ApprovalService(raw_storage_path, sync_report_status=False)
            if raw_storage_path is not None
            else approval_service
        )
        self._trace_service = trace_repository or trace_service
        self._report_generator = report_repository or report_generator

    async def start_after_approval(
        self,
        *,
        incident_id: str,
        change_plan_id: str,
        approval_id: str,
        mode: ChangeExecutionMode = "dry_run_only",
        operator: str = "operator",
        observe_window_seconds: int = 300,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Start or resume a safe change workflow authorized by an approval."""
        approval = self._load_approved_request(
            incident_id=incident_id,
            approval_id=approval_id,
            change_plan_id=change_plan_id,
        )
        plan = approval.change_plan
        if plan is None:
            raise ChangeExecutionStateError("approval does not include a change plan")

        existing = self._find_existing_execution(
            incident_id=incident_id,
            change_plan_id=change_plan_id,
            approval_id=approval_id,
        )
        if existing is not None:
            if existing.status == "dry_run_completed" and mode in {"manual_record", "sandbox"}:
                failed_resume = self._revalidate_existing_dry_run_resume(
                    execution=existing,
                    approval=approval,
                    plan=plan,
                    requested_mode=mode,
                    operator=operator,
                )
                if failed_resume is not None:
                    execution, trace_event = failed_resume
                    yield self._event_payload(
                        event_type="change_precheck",
                        stage="precheck_completed",
                        status=(
                            execution.pre_check.status if execution.pre_check else execution.status
                        ),
                        message=(
                            execution.pre_check.reason
                            if execution.pre_check
                            else "pre-check 未通过"
                        ),
                        execution=execution,
                        trace_event=trace_event,
                    )
                    yield self._complete_payload(
                        execution,
                        "pre-check 未通过，安全变更流程已停止",
                    )
                    return
            if existing.status == "dry_run_completed" and mode == "sandbox":
                execution = self._resume_existing_dry_run_to_sandbox(
                    execution=existing,
                    plan=plan,
                    operator=operator,
                    observe_window_seconds=observe_window_seconds,
                )
                sandbox_stage, sandbox_message = _sandbox_event_status_text(execution.status)
                yield self._event_payload(
                    event_type="change_observation",
                    stage=sandbox_stage,
                    status=(
                        execution.observation.status if execution.observation else execution.status
                    ),
                    message=sandbox_message,
                    execution=execution,
                )
                yield self._complete_payload(
                    execution,
                    _sandbox_complete_message(execution.status),
                )
                return
            resumed_existing = self._resume_existing_execution_for_mode(
                execution=existing,
                requested_mode=mode,
                operator=operator,
            )
            if resumed_existing is not None:
                execution, trace_event = resumed_existing
                yield self._event_payload(
                    event_type="change_execution",
                    stage="waiting_manual_execution",
                    status=execution.status,
                    message=(
                        "dry-run 已完成，现进入人工执行结果记录等待态；Agent 不直接执行生产写操作"
                    ),
                    execution=execution,
                    trace_event=trace_event,
                )
                yield self._complete_payload(
                    execution,
                    "安全变更流程已进入人工执行记录等待态",
                )
                return
            yield self._event_payload(
                event_type="change_report",
                stage="change_execution_existing",
                status=existing.status,
                message="已存在相同审批和变更计划的安全变更执行记录，返回现有状态",
                execution=existing,
            )
            yield self._event_payload(
                event_type="complete",
                stage="change_resume_complete",
                status=existing.status,
                message="安全变更流程幂等完成",
                execution=existing,
            )
            return

        execution = ChangeExecution(
            change_execution_id=_stable_change_execution_id(
                approval_id=approval_id,
                change_plan_id=change_plan_id,
            ),
            change_plan_id=change_plan_id,
            approval_id=approval_id,
            incident_id=incident_id,
            trace_id=str(approval.metadata.get("trace_id") or "trace-unknown"),
            mode=mode,
            execution_steps=list(plan.steps),
            created_by=operator,
        )
        execution, created = self._store.create_change_execution_once(execution)
        if not created:
            yield self._event_payload(
                event_type="change_report",
                stage="change_execution_existing",
                status=execution.status,
                message="已存在相同审批和变更计划的安全变更执行记录，返回现有状态",
                execution=execution,
            )
            yield self._event_payload(
                event_type="complete",
                stage="change_resume_complete",
                status=execution.status,
                message="安全变更流程幂等完成",
                execution=execution,
            )
            return

        execution = self._transition(
            execution,
            "precheck_running",
            expected_statuses={"created"},
        )
        trace_event = self._record_change_event(
            execution=execution,
            event_type="change_precheck",
            status="running",
            summary="Pre-check started for approved change plan",
            metadata={"operator": operator},
        )
        yield self._event_payload(
            event_type="change_precheck",
            stage="precheck_running",
            status="running",
            message="审批已通过，开始安全变更 pre-check",
            execution=execution,
            trace_event=trace_event,
        )

        pre_check = self.run_pre_checks(approval=approval, plan=plan)
        execution = execution.model_copy(
            update={
                "status": "precheck_failed" if pre_check.status == "failed" else "dry_run_running",
                "pre_check": pre_check,
                "updated_at": utc_now(),
            }
        )
        execution = self._save_execution(
            execution,
            expected_statuses={"precheck_running"},
        )
        trace_event = self._record_change_event(
            execution=execution,
            event_type="change_precheck",
            status="failed" if pre_check.status == "failed" else "success",
            summary=pre_check.reason,
            metadata={"pre_check": pre_check.model_dump(mode="json")},
        )
        yield self._event_payload(
            event_type="change_precheck",
            stage="precheck_completed",
            status=pre_check.status,
            message=pre_check.reason,
            execution=execution,
            trace_event=trace_event,
        )
        if pre_check.status == "failed":
            yield self._complete_payload(execution, "pre-check 未通过，安全变更流程已停止")
            return

        trace_event = self._record_change_event(
            execution=execution,
            event_type="change_dry_run",
            status="running",
            summary="Dry-run started for approved change plan",
            metadata={"operator": operator},
        )
        yield self._event_payload(
            event_type="change_dry_run",
            stage="dry_run_running",
            status="running",
            message="pre-check 通过，开始 dry-run 校验",
            execution=execution,
            trace_event=trace_event,
        )

        dry_run = self.run_dry_run(plan)
        dry_run_failed = dry_run.status == "failed"
        next_status = "dry_run_failed" if dry_run_failed else self._status_after_dry_run(mode, plan)
        execution = execution.model_copy(
            update={
                "status": next_status,
                "dry_run": dry_run,
                "updated_at": utc_now(),
            }
        )
        execution = self._save_execution(
            execution,
            expected_statuses={"dry_run_running"},
        )
        trace_event = self._record_change_event(
            execution=execution,
            event_type="change_dry_run",
            status="failed" if dry_run_failed else "success",
            summary=dry_run.reason,
            metadata={"dry_run": dry_run.model_dump(mode="json")},
        )
        yield self._event_payload(
            event_type="change_dry_run",
            stage="dry_run_completed",
            status=dry_run.status,
            message=dry_run.reason,
            execution=execution,
            trace_event=trace_event,
        )
        if dry_run_failed:
            yield self._complete_payload(execution, "dry-run 未通过，安全变更流程已停止")
            return

        if mode == "manual_record":
            trace_event = self._record_change_event(
                execution=execution,
                event_type="change_execution",
                status="waiting",
                summary="Waiting for operator to record manual execution result",
                metadata={"operator": operator},
            )
            yield self._event_payload(
                event_type="change_execution",
                stage="waiting_manual_execution",
                status=execution.status,
                message="dry-run 通过，等待人工提交执行结果；Agent 不直接执行生产写操作",
                execution=execution,
                trace_event=trace_event,
            )
            yield self._complete_payload(execution, "安全变更流程已进入人工执行记录等待态")
            return

        if mode == "sandbox":
            execution = self._run_sandbox_execution(
                execution=execution,
                plan=plan,
                observe_window_seconds=observe_window_seconds,
            )
            sandbox_stage, sandbox_message = _sandbox_event_status_text(execution.status)
            yield self._event_payload(
                event_type="change_observation",
                stage=sandbox_stage,
                status=execution.observation.status if execution.observation else execution.status,
                message=sandbox_message,
                execution=execution,
            )
            yield self._complete_payload(execution, _sandbox_complete_message(execution.status))
            return

        trace_event = self._record_change_event(
            execution=execution,
            event_type="change_report",
            status="success",
            summary="Dry-run only workflow completed; no production mutation executed",
            metadata={"operator": operator},
        )
        yield self._event_payload(
            event_type="change_report",
            stage="dry_run_only_completed",
            status=execution.status,
            message="dry-run-only 流程完成，未执行生产变更",
            execution=execution,
            trace_event=trace_event,
        )
        yield self._complete_payload(execution, "安全变更 dry-run-only 流程完成")

    def run_pre_checks(self, *, approval: ApprovalRequest, plan: ChangePlan) -> PreCheckResult:
        """Validate approval binding, plan status, freshness, and rollback coverage."""
        latest_report = self._report_generator.get_report(plan.incident_id)
        return build_pre_check_result(
            approval=approval,
            plan=plan,
            latest_report_status=latest_report.status if latest_report else "",
        )

    def run_dry_run(self, plan: ChangePlan) -> DryRunResult:
        """Validate steps without executing production mutations."""
        return build_dry_run_result(plan)

    def record_manual_result(
        self,
        change_execution_id: str,
        request: ManualExecutionResultRequest,
    ) -> ChangeExecution:
        """Record a human execution result and produce observation/rollback state."""
        execution = self.get_execution(change_execution_id)
        allowed_statuses = _manual_result_source_statuses(request.status)
        if execution.status not in allowed_statuses:
            raise ChangeExecutionStateError(
                f"change execution is {execution.status}, expected one of "
                f"{sorted(allowed_statuses)}"
            )
        approval = self._load_approved_request(
            incident_id=execution.incident_id,
            approval_id=execution.approval_id,
            change_plan_id=execution.change_plan_id,
        )
        if approval.decided_by and approval.decided_by == request.operator:
            raise ChangeExecutionStateError(
                "manual execution result must be recorded by an actor other than the approver"
            )
        plan = approval.change_plan
        if plan is None:
            raise ChangeExecutionStateError("approval does not include a change plan")
        pre_check = self.run_pre_checks(approval=approval, plan=plan)
        if pre_check.status == "failed":
            raise ChangeExecutionStateError(
                f"approved change plan is no longer valid: {pre_check.reason}"
            )

        recorded_at = utc_now()
        previous_manual_result = dict(execution.manual_result or {})
        manual_result = request.model_dump(mode="json")
        manual_result["recorded_at"] = recorded_at.isoformat()
        history = list(previous_manual_result.get("history") or [])
        if previous_manual_result:
            history.append(
                {key: value for key, value in previous_manual_result.items() if key != "history"}
            )
        if history:
            manual_result["history"] = history[-20:]
        _validate_manual_step_results(execution, request)
        observation = _manual_observation(execution, request)
        final_status = _status_from_manual_result(request.status)
        rollback_result: dict[str, Any] = {}
        if final_status in {"rollback_recommended", "partial_success", "rollback_failed"}:
            rollback_result = {
                "status": ("failed" if final_status == "rollback_failed" else "recommended"),
                "reason": _rollback_reason(final_status),
                "created_at": utc_now().isoformat(),
            }
        elif final_status == "rolled_back":
            rollback_result = {
                "status": "completed",
                "reason": "人工确认已按审批计划完成回滚，仍需持续观察恢复状态",
                "created_at": utc_now().isoformat(),
            }
        completed = execution.model_copy(
            update={
                "status": final_status,
                "manual_result": manual_result,
                "observation": observation,
                "rollback_result": rollback_result,
                "projection_pending": ["incident_state", "report", "trace"],
                "updated_at": recorded_at,
            }
        )
        if not self._store.save_change_execution_if_status(
            completed,
            expected_statuses=allowed_statuses,
        ):
            latest = self.get_execution(change_execution_id)
            raise ChangeExecutionStateError(
                f"change execution is {latest.status}, expected one of {sorted(allowed_statuses)}"
            )
        execution = self._sync_committed_execution(completed)
        self._record_change_event_best_effort(
            execution=execution,
            event_type="change_execution",
            status=_trace_status_from_manual_result(request.status),
            summary=f"Manual execution result recorded: {request.status}",
            metadata={"manual_result": manual_result},
        )
        self._record_change_event_best_effort(
            execution=execution,
            event_type="change_observation",
            status=(
                "success"
                if observation.status == "passed"
                else "waiting"
                if observation.status == "pending"
                else "failed"
            ),
            summary=observation.recommendation,
            metadata={"observation": observation.model_dump(mode="json")},
        )
        if rollback_result:
            self._record_change_event_best_effort(
                execution=execution,
                event_type=(
                    "change_rollback_completed"
                    if final_status == "rolled_back"
                    else "change_rollback_failed"
                    if final_status == "rollback_failed"
                    else "change_rollback_recommended"
                ),
                status=(
                    "success"
                    if final_status == "rolled_back"
                    else "failed"
                    if final_status == "rollback_failed"
                    else "blocked"
                ),
                summary=rollback_result["reason"],
                metadata={"rollback_result": rollback_result},
            )
        return execution

    def get_execution(self, change_execution_id: str) -> ChangeExecution:
        """Return one safe change execution."""
        execution = self._store.get_change_execution(change_execution_id)
        if execution is None:
            raise ChangeExecutionNotFoundError(change_execution_id)
        return self._repair_execution_if_needed(execution)

    def list_executions(
        self,
        *,
        incident_id: str | None = None,
        change_plan_id: str | None = None,
    ) -> list[ChangeExecution]:
        """List safe change executions."""
        executions = self._store.list_change_executions(
            incident_id=incident_id,
            change_plan_id=change_plan_id,
        )
        return [self._repair_execution_if_needed(execution) for execution in executions]

    def _load_approved_request(
        self,
        *,
        incident_id: str,
        approval_id: str,
        change_plan_id: str,
    ) -> ApprovalRequest:
        try:
            approval = self._approval_service.get_request(approval_id)
        except ApprovalNotFoundError:
            raise

        if approval.incident_id != incident_id:
            raise ChangeExecutionStateError("approval_id does not belong to the requested incident")
        if approval.status != "approved":
            raise ChangeExecutionStateError(f"approval is {approval.status}, expected approved")
        if self._approval_service.is_expired(approval):
            raise ChangeExecutionStateError("approval authorization has expired")
        if approval.change_plan is None:
            raise ChangeExecutionStateError("approval does not include a change plan")
        if approval.change_plan.change_plan_id != change_plan_id:
            raise ChangeExecutionStateError(
                "change_plan_id does not match the approved change plan"
            )
        return approval

    def _find_existing_execution(
        self,
        *,
        incident_id: str,
        change_plan_id: str,
        approval_id: str,
    ) -> ChangeExecution | None:
        executions = self._store.list_change_executions(
            incident_id=incident_id,
            change_plan_id=change_plan_id,
        )
        for execution in executions:
            if execution.approval_id == approval_id:
                return execution
        return None

    def _revalidate_existing_dry_run_resume(
        self,
        *,
        execution: ChangeExecution,
        approval: ApprovalRequest,
        plan: ChangePlan,
        requested_mode: ChangeExecutionMode,
        operator: str,
    ) -> tuple[ChangeExecution, Any] | None:
        """Re-run pre-check before a completed dry-run can continue."""
        pre_check = self.run_pre_checks(approval=approval, plan=plan)
        metadata = {
            "pre_check": pre_check.model_dump(mode="json"),
            "operator": operator,
            "requested_mode": requested_mode,
            "resumed_from_status": execution.status,
        }
        if pre_check.status != "failed":
            self._record_change_event(
                execution=execution,
                event_type="change_precheck",
                status="success",
                summary="Pre-check revalidated before resuming completed dry-run",
                metadata=metadata,
            )
            return None

        updated = execution.model_copy(
            update={
                "status": "precheck_failed",
                "pre_check": pre_check,
                "updated_at": utc_now(),
            }
        )
        updated = self._save_execution(
            updated,
            expected_statuses={"dry_run_completed"},
        )
        trace_event = self._record_change_event(
            execution=updated,
            event_type="change_precheck",
            status="failed",
            summary=pre_check.reason,
            metadata=metadata,
        )
        return updated, trace_event

    def _resume_existing_execution_for_mode(
        self,
        *,
        execution: ChangeExecution,
        requested_mode: ChangeExecutionMode,
        operator: str,
    ) -> tuple[ChangeExecution, Any] | None:
        """Allow a validated dry-run to continue into manual result recording."""
        if requested_mode != "manual_record" or execution.status != "dry_run_completed":
            return None
        updated = execution.model_copy(
            update={
                "mode": "manual_record",
                "status": "waiting_manual_execution",
                "updated_at": utc_now(),
            }
        )
        updated = self._save_execution(
            updated,
            expected_statuses={"dry_run_completed"},
        )
        trace_event = self._record_change_event(
            execution=updated,
            event_type="change_execution",
            status="waiting",
            summary="Dry-run already completed; waiting for operator to record manual result",
            metadata={"operator": operator, "resumed_from_status": execution.status},
        )
        return updated, trace_event

    def _resume_existing_dry_run_to_sandbox(
        self,
        *,
        execution: ChangeExecution,
        plan: ChangePlan,
        operator: str,
        observe_window_seconds: int,
    ) -> ChangeExecution:
        next_status = self._status_after_dry_run("sandbox", plan)
        updated = execution.model_copy(
            update={
                "mode": "sandbox",
                "status": next_status,
                "updated_at": utc_now(),
            }
        )
        updated = self._save_execution(
            updated,
            expected_statuses={"dry_run_completed"},
        )
        self._record_change_event(
            execution=updated,
            event_type="change_execution",
            status="blocked" if next_status == "escalated" else "running",
            summary="Dry-run already completed; sandbox execution requested",
            metadata={"operator": operator, "resumed_from_status": execution.status},
        )
        return self._run_sandbox_execution(
            execution=updated,
            plan=plan,
            observe_window_seconds=observe_window_seconds,
        )

    def _status_after_dry_run(self, mode: ChangeExecutionMode, plan: ChangePlan) -> str:
        return status_after_dry_run(mode, plan)

    def _run_sandbox_execution(
        self,
        *,
        execution: ChangeExecution,
        plan: ChangePlan,
        observe_window_seconds: int,
    ) -> ChangeExecution:
        if execution.status == "escalated":
            rollback_result = {
                "status": "not_started",
                "reason": "prod 环境未启用本地沙箱，禁止执行 sandbox 模式",
                "created_at": utc_now().isoformat(),
            }
            execution = execution.model_copy(
                update={
                    "rollback_result": rollback_result,
                    "updated_at": utc_now(),
                }
            )
            execution = self._save_execution(
                execution,
                expected_statuses={"escalated"},
            )
            self._record_change_event(
                execution=execution,
                event_type="change_execution",
                status="blocked",
                summary=rollback_result["reason"],
                metadata={"rollback_result": rollback_result},
            )
            return execution

        self._record_change_event(
            execution=execution,
            event_type="change_execution",
            status="running",
            summary="Sandbox execution started with local fixture adapter",
            metadata={"data_source": "sandbox"},
        )
        observation = ObservationResult(
            change_execution_id=execution.change_execution_id,
            status="passed",
            window_seconds=observe_window_seconds,
            metrics=dict.fromkeys(plan.observe_metrics, "sandbox_ok"),
            logs=["sandbox adapter completed; no production mutation executed"],
            success_criteria=list(plan.observe_metrics),
            recommendation="沙箱执行和观察通过，可由人工决定是否进入正式变更流程。",
        )
        execution = execution.model_copy(
            update={
                "status": "sandbox_validated",
                "observation": observation,
                "updated_at": utc_now(),
            }
        )
        execution = self._save_execution(
            execution,
            expected_statuses={"sandbox_executing"},
        )
        self._record_change_event(
            execution=execution,
            event_type="change_observation",
            status="success",
            summary=observation.recommendation,
            metadata={"observation": observation.model_dump(mode="json")},
        )
        return execution

    def _transition(
        self,
        execution: ChangeExecution,
        status: str,
        *,
        expected_statuses: set[str],
    ) -> ChangeExecution:
        return self._save_execution(
            execution.model_copy(update={"status": status, "updated_at": utc_now()}),
            expected_statuses=expected_statuses,
        )

    def _save_execution(
        self,
        execution: ChangeExecution,
        *,
        expected_statuses: set[str],
    ) -> ChangeExecution:
        execution = execution.model_copy(
            update={"projection_pending": ["incident_state", "report", "trace"]}
        )
        if not self._store.save_change_execution_if_status(
            execution,
            expected_statuses=expected_statuses,
        ):
            latest = self.get_execution(execution.change_execution_id)
            raise ChangeExecutionStateError(
                f"change execution is {latest.status}, expected one of {sorted(expected_statuses)}"
            )
        return self._sync_committed_execution(execution)

    def _sync_report(self, execution: ChangeExecution) -> bool:
        try:
            self._report_generator.mark_change_execution_updated(
                incident_id=execution.incident_id,
                execution=build_change_execution_read_model(execution),
            )
            return True
        except Exception as exc:
            logger.warning(
                "Change execution report synchronization failed: incident_id={}, "
                "change_execution_id={}, error={}",
                execution.incident_id,
                execution.change_execution_id,
                exc,
            )
            return False

    def _sync_committed_execution(self, execution: ChangeExecution) -> ChangeExecution:
        pending = self._sync_execution_projections(execution)
        updated = execution.model_copy(update={"projection_pending": sorted(set(pending))})
        if updated.projection_pending != execution.projection_pending:
            if not self._store.save_change_execution_if_status(
                updated,
                expected_statuses={execution.status},
            ):
                latest = self._store.get_change_execution(execution.change_execution_id)
                return latest or execution
        return updated

    def _sync_execution_projections(self, execution: ChangeExecution) -> list[str]:
        """Update rebuildable projections and return failed projection names."""
        pending: list[str] = []
        try:
            self._store.save_incident_state(build_incident_state_from_change_execution(execution))
        except Exception as exc:
            pending.append("incident_state")
            logger.warning(
                "Change execution incident-state projection failed: "
                "change_execution_id={}, error={}",
                execution.change_execution_id,
                exc,
            )
        if not self._sync_report(execution):
            pending.append("report")
        if not self._sync_execution_audit_projection(execution):
            pending.append("trace")
        return pending

    def _sync_execution_audit_projection(self, execution: ChangeExecution) -> bool:
        event_id = (
            f"change:projection:{execution.change_execution_id}:"
            f"{execution.updated_at.isoformat().replace('+00:00', 'Z')}"
        )[:128]
        try:
            self._trace_service.record_change_event(
                event_id=event_id,
                created_at=execution.updated_at,
                trace_id=execution.trace_id or "trace-unknown",
                incident_id=execution.incident_id,
                change_execution_id=execution.change_execution_id,
                change_plan_id=execution.change_plan_id,
                approval_id=execution.approval_id,
                event_type="change_execution_projection",
                status=execution.status,
                summary=f"Safe change durable status={execution.status}",
                metadata={"projection": True, "mode": execution.mode},
            )
            return True
        except Exception as exc:
            logger.warning(
                "Change execution audit projection failed: change_execution_id={}, error={}",
                execution.change_execution_id,
                exc,
            )
            return False

    def repair_pending_projections(self, change_execution_id: str) -> ChangeExecution:
        """Retry projections previously marked as incomplete."""
        execution = self._store.get_change_execution(change_execution_id)
        if execution is None:
            raise ChangeExecutionNotFoundError(change_execution_id)
        if not execution.projection_pending:
            return execution
        return self._sync_committed_execution(
            execution.model_copy(update={"projection_pending": []})
        )

    def _repair_execution_if_needed(self, execution: ChangeExecution) -> ChangeExecution:
        if not execution.projection_pending:
            return execution
        return self.repair_pending_projections(execution.change_execution_id)

    def _record_change_event_best_effort(self, **kwargs: Any):
        """Record an audit projection without invalidating a committed execution result."""
        try:
            return self._record_change_event(**kwargs)
        except Exception as exc:
            execution = kwargs["execution"]
            logger.warning(
                "Change execution trace projection failed: change_execution_id={}, error={}",
                execution.change_execution_id,
                exc,
            )
            return None

    def _record_change_event(
        self,
        *,
        execution: ChangeExecution,
        event_type: str,
        status: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
    ):
        try:
            return self._trace_service.record_change_event(
                trace_id=execution.trace_id or "trace-unknown",
                incident_id=execution.incident_id,
                change_execution_id=execution.change_execution_id,
                change_plan_id=execution.change_plan_id,
                approval_id=execution.approval_id,
                event_type=event_type,
                status=status,
                summary=summary,
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning(
                "Change execution trace projection failed: change_execution_id={}, error={}",
                execution.change_execution_id,
                exc,
            )
            return None

    def _event_payload(
        self,
        *,
        event_type: str,
        stage: str,
        status: str,
        message: str,
        execution: ChangeExecution,
        trace_event: Any | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": event_type,
            "stage": stage,
            "status": status,
            "message": message,
            "incident_id": execution.incident_id,
            "trace_id": execution.trace_id,
            "change_execution": build_change_execution_read_model(execution),
        }
        if trace_event is not None:
            payload["trace_event_id"] = trace_event.event_id
            payload["trace_event"] = trace_event.model_dump(mode="json")
        return payload

    def _complete_payload(self, execution: ChangeExecution, message: str) -> dict[str, Any]:
        return self._event_payload(
            event_type="complete",
            stage="change_resume_complete",
            status=execution.status,
            message=message,
            execution=execution,
        )


def _sandbox_event_status_text(status: str) -> tuple[str, str]:
    if status == "escalated":
        return (
            "sandbox_escalated",
            "prod 环境未启用本地沙箱，sandbox 模式已停止并转人工接管",
        )
    return "sandbox_observed", "沙箱执行和观察已完成"


def _sandbox_complete_message(status: str) -> str:
    if status == "escalated":
        return "安全变更 sandbox 流程已转人工接管，未执行生产变更"
    return "安全变更沙箱流程完成"


def _manual_observation(
    execution: ChangeExecution,
    request: ManualExecutionResultRequest,
) -> ObservationResult:
    if request.status in {"succeeded", "rolled_back"}:
        failed_criteria: list[str] = []
        status: CheckStatus = "passed"
        recommendation = (
            "人工确认回滚完成，恢复状态仍需持续观察。"
            if request.status == "rolled_back"
            else "人工执行结果已记录，观察通过，安全变更流程关闭。"
        )
    elif request.status == "recovery_pending":
        failed_criteria = []
        status = "pending"
        recommendation = "人工执行已记录，恢复效果尚待观察窗口确认。"
    elif request.status == "partial":
        failed_criteria = ["manual_execution_partial"]
        status = "failed"
        recommendation = "变更仅部分完成，需停止后续步骤并评估回滚或恢复方案。"
    else:
        failed_criteria = ["manual_execution_status"]
        status = "failed"
        recommendation = (
            "回滚执行失败，需立即升级并进入人工恢复。"
            if request.status == "rollback_failed"
            else "人工执行结果为失败，建议按回滚步骤处理并升级给值班负责人。"
        )

    return ObservationResult(
        change_execution_id=execution.change_execution_id,
        status=status,
        window_seconds=request.observe_window_seconds,
        metrics=dict(request.observed_metrics),
        logs=[request.notes] if request.notes else [],
        success_criteria=["manual_result_recorded"],
        failed_criteria=failed_criteria,
        recommendation=recommendation,
    )


def _validate_manual_step_results(
    execution: ChangeExecution,
    request: ManualExecutionResultRequest,
) -> None:
    expected_ids = {step.step_id for step in execution.execution_steps}
    actual_ids = {result.step_id for result in request.step_results}
    if len(actual_ids) != len(request.step_results):
        raise ChangeExecutionStateError("manual step results contain duplicate step_id values")
    if actual_ids - expected_ids:
        raise ChangeExecutionStateError("manual step results contain unapproved step_id values")
    if request.status == "partial" and len(expected_ids) > 1:
        statuses = {result.status for result in request.step_results}
        if actual_ids != expected_ids or "succeeded" not in statuses or statuses == {"succeeded"}:
            raise ChangeExecutionStateError(
                "multi-step partial result requires an outcome for every approved step "
                "and a mix of succeeded and incomplete outcomes"
            )
    if request.status == "succeeded" and len(expected_ids) > 1:
        if actual_ids != expected_ids or any(
            result.status != "succeeded" for result in request.step_results
        ):
            raise ChangeExecutionStateError(
                "multi-step success requires a succeeded result for every approved step"
            )


def _status_from_manual_result(status: str) -> str:
    return {
        "succeeded": "closed",
        "failed": "rollback_recommended",
        "partial": "partial_success",
        "recovery_pending": "recovery_pending",
        "rolled_back": "rolled_back",
        "rollback_failed": "rollback_failed",
    }[status]


def _manual_result_source_statuses(status: str) -> set[str]:
    transitions = {
        "succeeded": {"waiting_manual_execution", "recovery_pending"},
        "failed": {"waiting_manual_execution", "partial_success", "recovery_pending"},
        "partial": {"waiting_manual_execution"},
        "recovery_pending": {
            "waiting_manual_execution",
            "partial_success",
            "rollback_recommended",
            "rolled_back",
            "rollback_failed",
        },
        "rolled_back": {
            "waiting_manual_execution",
            "partial_success",
            "rollback_recommended",
            "rollback_failed",
        },
        "rollback_failed": {
            "waiting_manual_execution",
            "partial_success",
            "rollback_recommended",
        },
    }
    return transitions[status]


def _trace_status_from_manual_result(status: str) -> str:
    if status in {"succeeded", "rolled_back"}:
        return "success"
    if status == "recovery_pending":
        return "waiting"
    return "failed"


def _rollback_reason(status: str) -> str:
    if status == "rollback_failed":
        return "人工回滚失败，需立即升级并进入恢复待确认状态"
    if status == "partial_success":
        return "人工变更仅部分完成，建议停止后续步骤并评估回滚"
    return "人工执行失败或观察指标未达标，建议按 ChangePlan 回滚步骤处理"


def _stable_change_execution_id(*, approval_id: str, change_plan_id: str) -> str:
    """Return the same execution id for retries of one approved change plan."""
    digest = sha256(f"{approval_id}:{change_plan_id}".encode()).hexdigest()[:24]
    return f"chgexec-{digest}"


change_execution_service = ChangeExecutionService()
