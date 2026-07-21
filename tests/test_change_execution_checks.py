"""Unit tests for safe-change validation policy helpers."""

from datetime import timedelta

from app.models.approval import ApprovalRequest
from app.models.change_plan import ChangePlan, ChangeStep
from app.models.incident import utc_now
from app.services.approval_service import ApprovalService
from app.services.change_execution_checks import (
    build_dry_run_result,
    build_pre_check_result,
    status_after_dry_run,
)
from app.services.change_plan_builder import build_change_plan


def _approved_plan(**overrides) -> ChangePlan:
    values = {
        "incident_id": "inc-safe-change",
        "action": "调整 Redis maxclients",
        "risk_level": "high",
        "status": "approved",
        "rollback_steps": ["恢复 maxclients 到原值"],
        "observe_metrics": ["redis_connected_clients", "rejected_connections"],
        "blast_radius": "order-service",
        "metadata": {"environment": "prod", "service_name": "order-service"},
    }
    values.update(overrides)
    return ChangePlan(**values)


def _approved_request(plan: ChangePlan) -> ApprovalRequest:
    return ApprovalRequest(
        incident_id=plan.incident_id,
        action=plan.action,
        risk_level=plan.risk_level,
        status="approved",
        change_plan=plan,
        decided_at=utc_now(),
    )


def test_pre_check_result_captures_report_status_and_blocks_unsafe_stale_plan() -> None:
    plan = _approved_plan(
        rollback_steps=[],
        rollback_plan=[],
        created_at=utc_now() - timedelta(hours=2),
        expires_in_seconds=60,
    )

    result = build_pre_check_result(
        approval=_approved_request(plan),
        plan=plan,
        latest_report_status="approval_approved",
    )

    assert result.status == "failed"
    assert result.evidence_snapshot["latest_report_status"] == "approval_approved"
    assert any("过期窗口" in item for item in result.failed_items)
    assert any("缺少 rollback plan" in item for item in result.failed_items)


def test_dry_run_result_blocks_steps_that_cannot_be_dry_run() -> None:
    blocked_step = ChangeStep(
        step_id="step-blocked",
        action_type="manual_change",
        target="redis",
        tool_name="manual_change_record",
        expected_result="调整完成",
        risk_level="high",
        can_dry_run=False,
    )
    plan = _approved_plan(steps=[blocked_step])

    result = build_dry_run_result(plan)

    assert result.status == "failed"
    assert result.blocked_steps == ["step-blocked"]
    assert result.validated_steps == []
    assert "dry-run 阻断步骤" in result.reason


def test_status_after_dry_run_keeps_prod_sandbox_on_manual_boundary() -> None:
    prod_plan = _approved_plan(metadata={"environment": "prod"})
    staging_plan = _approved_plan(metadata={"environment": "staging"})

    assert status_after_dry_run("manual_record", prod_plan) == "waiting_manual_execution"
    assert status_after_dry_run("sandbox", prod_plan) == "escalated"
    assert status_after_dry_run("sandbox", staging_plan) == "sandbox_executing"
    assert status_after_dry_run("dry_run_only", prod_plan) == "dry_run_completed"


def test_sandbox_environment_boundary_is_fail_closed_for_aliases_and_unknown() -> None:
    for environment in ["prod-us", "production-east", "prd_east", "unknown", ""]:
        plan = _approved_plan(metadata={"environment": environment})
        assert status_after_dry_run("sandbox", plan) == "escalated"

    assert (
        status_after_dry_run(
            "sandbox",
            _approved_plan(metadata={"environment": "staging"}),
        )
        == "sandbox_executing"
    )


def test_dry_run_blocks_unapproved_tool_target_args_and_unknown_environment() -> None:
    unsafe_step = ChangeStep(
        step_id="step-unsafe-dry-run",
        action_type="service_restart",
        target="other-service",
        tool_name="restart_service",
        input_args={
            "environment": "unknown",
            "command": "kubectl rollout restart",
        },
        expected_result="service restarted",
        risk_level="high",
        can_dry_run=True,
    )
    plan = _approved_plan(
        steps=[unsafe_step],
        metadata={"environment": "unknown", "service_name": "order-service"},
    )

    result = build_dry_run_result(plan)

    assert result.status == "failed"
    assert result.blocked_steps == [unsafe_step.step_id]


def test_change_plan_builder_does_not_allow_input_args_to_override_scope() -> None:
    plan = build_change_plan(
        incident_id="inc-scope",
        action="restart order-service",
        risk_level="high",
        tool_name="suggest_remediation",
        service_name="order-service",
        environment="prod",
        input_args={
            "action": "change payment-service",
            "service_name": "payment-service",
            "environment": "staging",
        },
    )

    assert plan.steps[0].input_args["action"] == "restart order-service"
    assert plan.steps[0].input_args["service_name"] == "order-service"
    assert plan.steps[0].input_args["environment"] == "prod"


def test_pre_check_blocks_plan_step_scope_and_risk_drift(tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db")
    plan = _approved_plan()
    request = service.create_request(
        ApprovalRequest(
            incident_id=plan.incident_id,
            action=plan.action,
            risk_level=plan.risk_level,
            change_plan=plan.model_copy(update={"status": "draft"}),
        )
    )
    approved = service.decide_request(request.approval_id, decision="approve")
    assert approved.change_plan is not None
    drifted_step = ChangeStep(
        action_type="service_restart",
        target="other-service",
        input_args={"environment": "prod"},
        expected_result="service restarted",
        risk_level="low",
        requires_approval=False,
    ).model_copy(
        update={
            "step_id": "step-drifted",
        }
    )
    drifted_plan = approved.change_plan.model_copy(update={"steps": [drifted_step]})

    result = build_pre_check_result(approval=approved, plan=drifted_plan)

    assert result.status == "failed"
    assert any("内容已变更" in item for item in result.failed_items)
    assert any("风险等级" in item for item in result.failed_items)
    assert any("未绑定人工审批" in item for item in result.failed_items)
    assert any("服务范围" in item for item in result.failed_items)


def test_pre_check_blocks_cross_target_high_risk_rollback(tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db")
    execution_step = ChangeStep(
        step_id="execute-prod",
        action_type="service_restart",
        target="order-service",
        tool_name="manual_change_record",
        input_args={"environment": "prod"},
        risk_level="high",
        rollback_step_id="rollback-prod",
    )
    rollback_step = ChangeStep(
        step_id="rollback-prod",
        action_type="manual_rollback",
        target="payment-service",
        tool_name="manual_change_record",
        input_args={"environment": "prod"},
        risk_level="high",
    )
    plan = _approved_plan(
        steps=[execution_step],
        rollback_plan=[rollback_step],
        rollback_steps=[],
    )
    pending = service.create_request(
        ApprovalRequest(
            incident_id=plan.incident_id,
            action=plan.action,
            risk_level=plan.risk_level,
            change_plan=plan.model_copy(update={"status": "draft"}),
        )
    )
    approved = service.decide_request(pending.approval_id, decision="approve")
    assert approved.change_plan is not None

    result = build_pre_check_result(approval=approved, plan=approved.change_plan)

    assert result.status == "failed"
    assert any("回滚步骤的目标" in item for item in result.failed_items)
