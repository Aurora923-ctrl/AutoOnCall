"""Model and builder tests for safe change workflows."""

from app.models.change_execution import ChangeExecution, DryRunResult, PreCheckResult
from app.services.change_execution_read_models import build_change_execution_read_model
from app.services.change_plan_builder import build_change_plan


def test_change_plan_builder_creates_structured_redis_plan() -> None:
    plan = build_change_plan(
        incident_id="inc-redis",
        action="人工调整 Redis maxclients",
        risk_level="high",
        tool_name="suggest_remediation",
        service_name="order-service",
        environment="prod",
        reason="Redis connected_clients 接近 maxclients",
    )

    assert plan.steps
    assert plan.steps[0].action_type == "redis_config_change"
    assert plan.steps[0].requires_approval is True
    assert plan.rollback_plan
    assert plan.steps[0].rollback_step_id == plan.rollback_plan[0].step_id
    assert "redis_connected_clients" in plan.observe_metrics
    assert plan.blast_radius == "prod/order-service"
    assert plan.manual_execution_required is True


def test_change_execution_models_round_trip_json_payload() -> None:
    execution = ChangeExecution(
        change_plan_id="chg-1",
        approval_id="apr-1",
        incident_id="inc-1",
        trace_id="trace-1",
        status="dry_run_failed",
        pre_check=PreCheckResult(
            change_plan_id="chg-1",
            status="passed",
            checked_items=["approval ok"],
            reason="pre-check 通过",
        ),
        dry_run=DryRunResult(
            change_plan_id="chg-1",
            status="failed",
            blocked_steps=["s1"],
            reason="dry-run 阻断步骤：s1",
        ),
    )

    payload = execution.model_dump(mode="json")
    loaded = ChangeExecution.model_validate(payload)

    assert loaded.change_execution_id == execution.change_execution_id
    assert loaded.pre_check is not None
    assert loaded.pre_check.status == "passed"
    assert loaded.dry_run is not None
    assert loaded.dry_run.blocked_steps == ["s1"]


def test_change_execution_read_model_exposes_lifecycle_and_stages() -> None:
    execution = ChangeExecution(
        change_plan_id="chg-1",
        approval_id="apr-1",
        incident_id="inc-1",
        trace_id="trace-1",
        status="closed",
        pre_check=PreCheckResult(
            change_plan_id="chg-1",
            status="passed",
            checked_items=["approval ok"],
            reason="pre-check 通过",
        ),
        dry_run=DryRunResult(
            change_plan_id="chg-1",
            status="passed",
            validated_steps=["s1"],
            reason="dry-run 校验通过",
        ),
    )

    payload = build_change_execution_read_model(execution)

    assert payload["lifecycle_status"] == "resolved"
    assert payload["status_metadata"]["tone"] == "success"
    assert [stage["key"] for stage in payload["stages"]] == [
        "pre_check",
        "dry_run",
        "execute",
        "observe",
    ]
    assert payload["stages"][2]["status"] == "skipped"
    assert "安全变更流程已关闭" in payload["next_steps"][0]
