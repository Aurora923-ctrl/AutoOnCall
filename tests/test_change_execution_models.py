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
    assert plan.remediation_playbook is not None
    assert plan.remediation_playbook.risk_policy == "approval_required"
    assert "Redis maxclients" in plan.remediation_playbook.summary
    assert any("CONFIG SET" in item for item in plan.remediation_playbook.safety_notes)
    assert any("incident-window" in item for item in plan.remediation_playbook.safety_notes)
    assert plan.blast_radius == "prod/order-service"
    assert plan.manual_execution_required is True


def test_change_plan_builder_creates_mysql_playbook_without_auto_sql() -> None:
    plan = build_change_plan(
        incident_id="inc-mysql",
        action="人工优化 MySQL slow query",
        risk_level="high",
        tool_name="suggest_remediation",
        service_name="payment-service",
        environment="prod",
        reason="MySQL 慢查询导致 P95 升高",
    )

    assert plan.remediation_playbook is not None
    assert "MySQL 慢查询" in plan.remediation_playbook.summary
    assert "mysql_slow_query_count" in plan.remediation_playbook.observe_metrics
    assert any("不执行生产 DDL/DML" in item for item in plan.remediation_playbook.dry_run)
    assert any("不自动执行生产 SQL" in item for item in plan.remediation_playbook.safety_notes)


def test_prod_redis_playbook_requires_approval_even_when_risk_is_low() -> None:
    plan = build_change_plan(
        incident_id="inc-redis-low",
        action="调整 Redis maxclients",
        risk_level="low",
        service_name="order-service",
        environment="prod",
    )

    assert plan.remediation_playbook is not None
    assert plan.remediation_playbook.risk_policy == "approval_required"
    assert plan.remediation_playbook.approval_required is True
    assert plan.steps[0].requires_approval is True


def test_chinese_mysql_slow_query_action_builds_database_playbook() -> None:
    plan = build_change_plan(
        incident_id="inc-mysql-cn",
        action="人工处理慢查询和连接池参数",
        risk_level="low",
        service_name="payment-service",
        environment="prod",
    )

    assert plan.steps[0].action_type == "database_change"
    assert plan.remediation_playbook is not None
    assert "MySQL 慢查询" in plan.remediation_playbook.summary
    assert plan.remediation_playbook.risk_policy == "approval_required"


def test_change_plan_uses_reason_and_metadata_for_playbook_domain() -> None:
    plan = build_change_plan(
        incident_id="inc-mysql-reason",
        action="人工生成处置方案",
        risk_level="low",
        service_name="payment-service",
        environment="prod",
        reason="慢查询和 Threads_running 升高导致连接池耗尽",
        metadata={"golden_chain": "mysql_slow_query_latency"},
    )

    assert plan.steps[0].action_type == "database_change"
    assert plan.remediation_playbook is not None
    assert "MySQL 慢查询" in plan.remediation_playbook.summary
    assert "mysql_slow_query_count" in plan.remediation_playbook.observe_metrics


def test_change_plan_uses_metadata_keys_for_playbook_domain() -> None:
    plan = build_change_plan(
        incident_id="inc-redis-key",
        action="人工生成处置方案",
        risk_level="low",
        service_name="order-service",
        environment="prod",
        metadata={"redis_maxclients": True},
    )

    assert plan.steps[0].action_type == "redis_config_change"
    assert plan.remediation_playbook is not None
    assert "Redis maxclients" in plan.remediation_playbook.summary


def test_non_prod_low_risk_change_plan_still_uses_manual_approval_boundary() -> None:
    plan = build_change_plan(
        incident_id="inc-generic-low",
        action="人工调整测试环境配置",
        risk_level="low",
        service_name="demo-service",
        environment="staging",
    )

    assert plan.remediation_playbook is not None
    assert plan.remediation_playbook.risk_policy == "approval_required"
    assert plan.remediation_playbook.approval_required is True
    assert plan.steps[0].requires_approval is True


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
        status="dry_run_completed",
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

    assert payload["lifecycle_status"] == "change_validated"
    assert payload["status_metadata"]["tone"] == "success"
    assert [stage["key"] for stage in payload["stages"]] == [
        "pre_check",
        "dry_run",
        "execute",
        "observe",
    ]
    assert payload["stages"][2]["status"] == "skipped"
    assert "dry-run 已完成" in payload["next_steps"][0]
