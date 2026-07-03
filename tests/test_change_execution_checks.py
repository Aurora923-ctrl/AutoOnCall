"""Unit tests for safe-change validation policy helpers."""

from datetime import timedelta

from app.models.approval import ApprovalRequest
from app.models.change_plan import ChangePlan, ChangeStep
from app.models.incident import utc_now
from app.services.change_execution_checks import (
    build_dry_run_result,
    build_pre_check_result,
    status_after_dry_run,
)


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
