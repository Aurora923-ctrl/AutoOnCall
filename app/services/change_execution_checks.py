"""Validation policy for approved safe-change workflows."""

from __future__ import annotations

from datetime import UTC

from app.models.approval import ApprovalRequest
from app.models.change_execution import (
    ChangeExecutionMode,
    CheckStatus,
    DryRunResult,
    PreCheckResult,
)
from app.models.change_plan import ChangePlan, ChangeStep
from app.models.incident import utc_now
from app.services.incident_lifecycle import is_production_environment


def build_pre_check_result(
    *,
    approval: ApprovalRequest,
    plan: ChangePlan,
    latest_report_status: str = "",
) -> PreCheckResult:
    """Validate approval binding, plan freshness, and rollback coverage."""
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
        not is_expired(plan),
        "ChangePlan 未超过过期窗口",
        "ChangePlan 已超过过期窗口，需要重新诊断和审批",
    )
    has_rollback = bool(plan.rollback_steps or plan.rollback_plan)
    check(
        plan.risk_level != "high" or has_rollback,
        "高风险变更包含回滚方案",
        "高风险变更缺少 rollback plan，禁止进入 dry-run",
    )
    checked_items.extend(
        [
            "目标服务、环境、动作、审批记录已从持久化快照校验",
            "第一版不重新执行生产写操作，只进入 dry-run/sandbox/manual_record 边界",
        ]
    )

    evidence_snapshot = {
        "approval_id": approval.approval_id,
        "approval_status": approval.status,
        "approval_decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "change_plan_id": plan.change_plan_id,
        "change_plan_status": plan.status,
        "risk_level": plan.risk_level,
        "blast_radius": plan.blast_radius,
        "observe_metrics": list(plan.observe_metrics),
        "latest_report_status": latest_report_status,
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


def build_dry_run_result(plan: ChangePlan) -> DryRunResult:
    """Validate steps without executing production mutations."""
    steps = plan_steps(plan)
    blocked_steps = [step.step_id for step in steps if not step.can_dry_run]
    validated_steps = [step.step_id for step in steps if step.step_id not in blocked_steps]
    if plan.metadata.get("dry_run_should_fail") or plan.metadata.get("force_dry_run_failure"):
        blocked_steps.append("metadata:dry_run_should_fail")

    if not steps and plan.execution_steps:
        validated_steps = [
            f"execution_steps[{index}]" for index, _ in enumerate(plan.execution_steps)
        ]

    status: CheckStatus = "failed" if blocked_steps else "passed"
    diff_preview = dry_run_diff_preview(plan)
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


def status_after_dry_run(mode: ChangeExecutionMode, plan: ChangePlan) -> str:
    """Choose the next workflow state after a successful dry-run."""
    if mode == "manual_record":
        return "waiting_manual_execution"
    if mode == "sandbox":
        environment = str(plan.metadata.get("environment") or "").lower()
        if is_production_environment(environment) and not plan.metadata.get("sandbox_enabled"):
            return "escalated"
        return "sandbox_executing"
    return "dry_run_completed"


def is_expired(plan: ChangePlan) -> bool:
    created_at = plan.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return (utc_now() - created_at).total_seconds() > max(plan.expires_in_seconds, 1)


def plan_steps(plan: ChangePlan) -> list[ChangeStep]:
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


def dry_run_diff_preview(plan: ChangePlan) -> list[str]:
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
