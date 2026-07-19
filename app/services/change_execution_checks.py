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
from app.models.change_plan import ChangePlan, ChangeStep, change_plan_fingerprint
from app.models.incident import utc_now

DRY_RUN_TOOL_ALLOWLIST = {"manual_change_record", "suggest_remediation"}
DRY_RUN_ACTION_ALLOWLIST = {
    "manual",
    "manual_change",
    "manual_rollback",
    "redis_config_change",
    "database_change",
    "service_restart",
    "capacity_change",
    "release_rollback",
}
DRY_RUN_INPUT_ALLOWLIST = {
    "action",
    "description",
    "environment",
    "service_name",
    "source_action",
}
NON_PRODUCTION_ENVIRONMENTS = {
    "dev",
    "development",
    "test",
    "testing",
    "qa",
    "staging",
    "stage",
    "sandbox",
    "local",
}


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
    expected_plan_fingerprint = str(approval.metadata.get("change_plan_fingerprint") or "")
    actual_plan_fingerprint = change_plan_fingerprint(plan)
    check(
        bool(expected_plan_fingerprint) and expected_plan_fingerprint == actual_plan_fingerprint,
        "审批绑定的 ChangePlan 指纹一致",
        "审批绑定的 ChangePlan 内容已变更或缺少指纹",
    )
    check(
        approval.action == plan.action,
        "审批动作与 ChangePlan 动作一致",
        "审批动作与 ChangePlan 动作不一致",
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
    steps = plan_steps(plan)
    check(
        bool(steps),
        "ChangePlan 包含可审计的变更步骤",
        "ChangePlan 不包含变更步骤",
    )
    check(
        all(step.risk_level == plan.risk_level for step in steps),
        "所有变更步骤风险等级与 ChangePlan 一致",
        "变更步骤风险等级与 ChangePlan 不一致",
    )
    check(
        all(step.requires_approval for step in steps),
        "所有变更步骤均绑定人工审批",
        "存在未绑定人工审批的变更步骤",
    )
    plan_service_name = str(plan.metadata.get("service_name") or "")
    check(
        not plan_service_name
        or all(not step.target or step.target == plan_service_name for step in steps),
        "所有变更步骤目标与 ChangePlan 服务范围一致",
        "变更步骤目标超出 ChangePlan 服务范围",
    )
    plan_environment = str(plan.metadata.get("environment") or "")
    check(
        all(
            not step.input_args.get("environment")
            or str(step.input_args.get("environment")) == plan_environment
            for step in steps
        ),
        "所有变更步骤环境与 ChangePlan 一致",
        "变更步骤环境超出 ChangePlan 审批范围",
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
    if plan.risk_level == "high" and plan.steps:
        rollback_steps = {step.step_id: step for step in plan.rollback_plan}
        linked_rollbacks = [
            rollback_steps.get(str(step.rollback_step_id or "")) for step in plan.steps
        ]
        check(
            all(rollback is not None for rollback in linked_rollbacks),
            "所有高风险结构化步骤均关联可定位的回滚步骤",
            "高风险结构化步骤缺少有效 rollback_step_id 映射",
        )
        check(
            all(
                rollback is not None
                and rollback.target == step.target
                and rollback.risk_level == step.risk_level
                and rollback.requires_approval
                for step, rollback in zip(steps, linked_rollbacks, strict=True)
            ),
            "执行步骤与回滚步骤的目标、风险和审批边界一致",
            "执行步骤与回滚步骤的目标、风险或审批边界不一致",
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
        "change_plan_fingerprint": actual_plan_fingerprint,
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
    blocked_steps = [
        step.step_id for step in steps if _dry_run_step_block_reasons(step=step, plan=plan)
    ]
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
        environment = normalize_environment(plan.metadata.get("environment"))
        if not is_known_non_production_environment(environment):
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


def normalize_environment(value: object) -> str:
    """Return a normalized environment name for strict boundary checks."""
    return str(value or "").strip().lower()


def is_known_non_production_environment(value: object) -> bool:
    """Allow sandbox execution only for an explicit non-production environment."""
    environment = normalize_environment(value)
    return environment in NON_PRODUCTION_ENVIRONMENTS


def _dry_run_step_block_reasons(*, step: ChangeStep, plan: ChangePlan) -> list[str]:
    """Return fail-closed policy violations for one dry-run step."""
    reasons: list[str] = []
    plan_target = str(plan.metadata.get("service_name") or "").strip()
    plan_environment = normalize_environment(plan.metadata.get("environment"))
    step_environment = normalize_environment(step.input_args.get("environment"))

    if not step.can_dry_run:
        reasons.append("step_declares_no_dry_run")
    if step.tool_name not in DRY_RUN_TOOL_ALLOWLIST:
        reasons.append("tool_not_allowlisted")
    if step.action_type not in DRY_RUN_ACTION_ALLOWLIST:
        reasons.append("action_not_allowlisted")
    if not plan_target or not step.target or step.target != plan_target:
        reasons.append("target_not_allowlisted")
    if not plan_environment or plan_environment == "unknown":
        reasons.append("environment_unknown")
    if step_environment and step_environment != plan_environment:
        reasons.append("environment_scope_mismatch")
    if set(step.input_args) - DRY_RUN_INPUT_ALLOWLIST:
        reasons.append("input_args_not_allowlisted")
    return reasons


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
