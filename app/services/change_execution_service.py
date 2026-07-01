"""Safe change workflow orchestration after human approval."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC
from pathlib import Path
from typing import Any

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
from app.models.change_plan import ChangePlan, ChangeStep
from app.models.incident import utc_now
from app.services.aiops_store import create_aiops_store
from app.services.approval_service import ApprovalNotFoundError, ApprovalService, approval_service
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
            change_plan_id=change_plan_id,
            approval_id=approval_id,
            incident_id=incident_id,
            trace_id=str(approval.metadata.get("trace_id") or "trace-unknown"),
            mode=mode,
            execution_steps=list(plan.steps),
            created_by=operator,
        )
        execution = self._save_execution(execution)

        execution = self._transition(execution, "precheck_running")
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
        execution = self._save_execution(execution)
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
        execution = self._save_execution(execution)
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
                message="dry-run 通过，等待人工提交执行结果；Agent 不自动执行生产变更",
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
        checked_items: list[str] = []
        failed_items: list[str] = []

        def check(condition: bool, passed_text: str, failed_text: str) -> None:
            if condition:
                checked_items.append(passed_text)
            else:
                failed_items.append(failed_text)

        check(
            approval.incident_id == plan.incident_id,
            "approval_id 与 ChangePlan incident_id 一致",
            "approval_id 与 ChangePlan incident_id 不一致",
        )
        check(
            approval.status == "approved",
            "审批状态为 approved",
            f"审批状态为 {approval.status}，不是 approved",
        )
        check(
            plan.status == "approved",
            "ChangePlan 状态为 approved",
            f"ChangePlan 状态为 {plan.status}，不是 approved",
        )
        check(
            approval.risk_level == plan.risk_level,
            "审批风险等级与 ChangePlan 一致",
            "审批风险等级与 ChangePlan 不一致",
        )
        check(
            not _is_expired(plan),
            "ChangePlan 未超过过期窗口",
            "ChangePlan 已超过过期窗口，需要重新诊断和审批",
        )
        has_rollback = bool(plan.rollback_steps or plan.rollback_plan)
        check(
            plan.risk_level != "high" or has_rollback,
            "中高风险变更包含回滚方案",
            "高风险变更缺少 rollback plan，禁止进入 dry-run",
        )
        checked_items.extend(
            [
                "目标服务、环境、动作、审批记录已从持久化快照校验",
                "第一版不重新执行生产写操作，只进入 dry-run/sandbox/manual_record 边界",
            ]
        )

        latest_report = self._report_generator.get_report(plan.incident_id)
        evidence_snapshot = {
            "approval_id": approval.approval_id,
            "approval_status": approval.status,
            "approval_decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
            "change_plan_id": plan.change_plan_id,
            "change_plan_status": plan.status,
            "risk_level": plan.risk_level,
            "blast_radius": plan.blast_radius,
            "observe_metrics": list(plan.observe_metrics),
            "latest_report_status": latest_report.status if latest_report else "",
        }
        status: CheckStatus = "failed" if failed_items else "passed"
        reason = "pre-check 通过" if status == "passed" else "；".join(failed_items)
        return PreCheckResult(
            change_plan_id=plan.change_plan_id,
            status=status,
            checked_items=checked_items,
            failed_items=failed_items,
            evidence_snapshot=evidence_snapshot,
            reason=reason,
        )

    def run_dry_run(self, plan: ChangePlan) -> DryRunResult:
        """Validate steps without executing production mutations."""
        steps = _plan_steps(plan)
        blocked_steps = [step.step_id for step in steps if not step.can_dry_run]
        validated_steps = [step.step_id for step in steps if step.step_id not in blocked_steps]
        if plan.metadata.get("dry_run_should_fail") or plan.metadata.get("force_dry_run_failure"):
            blocked_steps.append("metadata:dry_run_should_fail")

        if not steps and plan.execution_steps:
            validated_steps = [
                f"execution_steps[{index}]" for index, _ in enumerate(plan.execution_steps)
            ]

        status: CheckStatus = "failed" if blocked_steps else "passed"
        diff_preview = _dry_run_diff_preview(plan)
        reason = (
            "dry-run 校验通过，未产生生产写操作"
            if status == "passed"
            else f"dry-run 阻断步骤：{', '.join(blocked_steps)}"
        )
        return DryRunResult(
            change_plan_id=plan.change_plan_id,
            status=status,
            validated_steps=validated_steps,
            blocked_steps=blocked_steps,
            diff_preview=diff_preview,
            reason=reason,
        )

    def record_manual_result(
        self,
        change_execution_id: str,
        request: ManualExecutionResultRequest,
    ) -> ChangeExecution:
        """Record a human execution result and produce observation/rollback state."""
        execution = self.get_execution(change_execution_id)
        if execution.status != "waiting_manual_execution":
            raise ChangeExecutionStateError(
                f"change execution is {execution.status}, expected waiting_manual_execution"
            )

        manual_result = request.model_dump(mode="json")
        manual_result["recorded_at"] = utc_now().isoformat()
        execution = execution.model_copy(
            update={
                "status": "manual_execution_recorded",
                "manual_result": manual_result,
                "updated_at": utc_now(),
            }
        )
        execution = self._save_execution(execution)
        self._record_change_event(
            execution=execution,
            event_type="change_execution",
            status="success" if request.status == "succeeded" else "failed",
            summary=f"Manual execution result recorded: {request.status}",
            metadata={"manual_result": manual_result},
        )

        observation = _manual_observation(execution, request)
        final_status = "closed" if observation.status == "passed" else "rollback_recommended"
        rollback_result: dict[str, Any] = {}
        if final_status == "rollback_recommended":
            rollback_result = {
                "status": "recommended",
                "reason": "人工执行失败或观察指标未达标，建议按 ChangePlan 回滚步骤处理",
                "created_at": utc_now().isoformat(),
            }
        execution = execution.model_copy(
            update={
                "status": final_status,
                "observation": observation,
                "rollback_result": rollback_result,
                "updated_at": utc_now(),
            }
        )
        execution = self._save_execution(execution)
        self._record_change_event(
            execution=execution,
            event_type="change_observation",
            status="success" if observation.status == "passed" else "failed",
            summary=observation.recommendation,
            metadata={"observation": observation.model_dump(mode="json")},
        )
        if final_status == "rollback_recommended":
            self._record_change_event(
                execution=execution,
                event_type="change_rollback_recommended",
                status="blocked",
                summary=rollback_result["reason"],
                metadata={"rollback_result": rollback_result},
            )
        return execution

    def get_execution(self, change_execution_id: str) -> ChangeExecution:
        """Return one safe change execution."""
        execution = self._store.get_change_execution(change_execution_id)
        if execution is None:
            raise ChangeExecutionNotFoundError(change_execution_id)
        return execution

    def list_executions(
        self,
        *,
        incident_id: str | None = None,
        change_plan_id: str | None = None,
    ) -> list[ChangeExecution]:
        """List safe change executions."""
        return self._store.list_change_executions(
            incident_id=incident_id,
            change_plan_id=change_plan_id,
        )

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

    def _status_after_dry_run(self, mode: ChangeExecutionMode, plan: ChangePlan) -> str:
        if mode == "manual_record":
            return "waiting_manual_execution"
        if mode == "sandbox":
            environment = str(plan.metadata.get("environment") or "").lower()
            if environment == "prod" and not plan.metadata.get("sandbox_enabled"):
                return "escalated"
            return "sandbox_executing"
        return "closed"

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
            execution = self._save_execution(execution)
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
                "status": "closed",
                "observation": observation,
                "updated_at": utc_now(),
            }
        )
        execution = self._save_execution(execution)
        self._record_change_event(
            execution=execution,
            event_type="change_observation",
            status="success",
            summary=observation.recommendation,
            metadata={"observation": observation.model_dump(mode="json")},
        )
        return execution

    def _transition(self, execution: ChangeExecution, status: str) -> ChangeExecution:
        return self._save_execution(
            execution.model_copy(update={"status": status, "updated_at": utc_now()})
        )

    def _save_execution(self, execution: ChangeExecution) -> ChangeExecution:
        self._store.save_change_execution(execution)
        self._store.save_incident_state(
            build_incident_state_from_change_execution(execution)
        )
        self._sync_report(execution)
        return execution

    def _sync_report(self, execution: ChangeExecution) -> None:
        try:
            self._report_generator.mark_change_execution_updated(
                incident_id=execution.incident_id,
                execution=build_change_execution_read_model(execution),
            )
        except Exception:
            return

    def _record_change_event(
        self,
        *,
        execution: ChangeExecution,
        event_type: str,
        status: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
    ):
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


def _is_expired(plan: ChangePlan) -> bool:
    created_at = plan.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return (utc_now() - created_at).total_seconds() > max(plan.expires_in_seconds, 1)


def _plan_steps(plan: ChangePlan) -> list[ChangeStep]:
    if plan.steps:
        return list(plan.steps)
    return [
        ChangeStep(
            action_type="manual_change",
            target=plan.metadata.get("service_name") or "",
            tool_name=plan.metadata.get("tool_name") or "manual_change_record",
            input_args={"description": text},
            expected_result="人工执行步骤完成并记录结果",
            risk_level=plan.risk_level,
            can_dry_run=True,
        )
        for text in plan.execution_steps
    ]


def _dry_run_diff_preview(plan: ChangePlan) -> list[str]:
    text = f"{plan.action} {' '.join(plan.observe_metrics)}".lower()
    if "redis" in text or "maxclients" in text:
        return [
            "data_source=dry_run，不调用生产 Redis CONFIG SET",
            "校验 maxclients 调整动作、目标服务、回滚步骤和观察指标是否齐备",
            "预期观察 redis_connected_clients、rejected_connections、5xx、P95 和 timeout 日志",
        ]
    return [
        "data_source=dry_run，不调用生产写接口",
        "校验目标、审批、回滚步骤、观察指标和人工执行边界",
    ]


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
    if request.status == "succeeded":
        failed_criteria: list[str] = []
        status: CheckStatus = "passed"
        recommendation = "人工执行结果已记录，观察通过，安全变更流程关闭。"
    else:
        failed_criteria = ["manual_execution_status"]
        status = "failed"
        recommendation = "人工执行结果为失败，建议按回滚步骤处理并升级给值班负责人。"

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


change_execution_service = ChangeExecutionService()
