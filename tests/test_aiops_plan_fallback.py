"""Tests for deterministic structured AIOps planning fallback."""

from app.agent.aiops.plan_fallback import (
    build_fallback_plan,
    ensure_unique_step_ids,
    normalize_plan_steps,
    render_plan_step,
)
from app.agent.aiops.risk_controller import assess_plan_step
from app.models.plan import PlanStep


def test_redis_timeout_fallback_plan_contains_expected_tools() -> None:
    steps = build_fallback_plan(
        input_text="order-service 最近 10 分钟 5xx 错误率升高，并出现 Redis connection timeout",
        incident={
            "service_name": "order-service",
            "symptom": "Redis connection timeout and P95 latency high",
        },
    )

    tool_names = [step.tool_name for step in steps]

    assert tool_names[:4] == [
        "query_service_context",
        "query_redis_status",
        "query_metrics",
        "query_logs",
    ]
    assert "query_deploy_history" in tool_names
    assert "search_runbook" in tool_names
    assert "search_history_ticket" in tool_names
    assert steps[-1].risk_level == "medium"
    assert all(step.status == "pending" for step in steps)
    assert all(step.step_id == f"s{index}" for index, step in enumerate(steps, 1))


def test_mysql_slow_query_fallback_plan_contains_mysql_step() -> None:
    steps = build_fallback_plan(
        input_text="billing-service 出现 MySQL 慢查询和连接池耗尽",
        incident=None,
    )

    assert steps[0].input_args["service_name"] == "billing-service"
    assert any(step.tool_name == "query_mysql_status" for step in steps)
    assert any(step.tool_name == "search_history_ticket" for step in steps)
    assert any("MySQL" in step.purpose for step in steps)


def test_mysql_demo_plan_uses_release_correlation_and_no_write_action() -> None:
    incident = {
        "service_name": "payment-service",
        "environment": "prod",
        "symptom": "P95 high, slow SQL digest and pool waiting after report feature flag",
        "raw_alert": {
            "alertname": "MySQLSlowQueryLatency",
            "dependency": "payment-mysql",
            "feature_flag": "PAYMENT_REPORT_ENABLED=true",
        },
    }

    steps = build_fallback_plan("payment-service MySQL slow query latency", incident)
    tool_names = [step.tool_name for step in steps]

    assert tool_names == [
        "query_mysql_status",
        "query_metrics",
        "query_logs",
        "query_deploy_history",
        "search_runbook",
        "search_history_ticket",
        "suggest_remediation",
    ]
    assert steps[2].input_args["service_name"] == "payment-service"
    assert "digest" in steps[2].input_args["query"]
    assert steps[5].input_args["service_name"] == "payment-service"
    assert "feature flag" in steps[5].input_args["query"].lower()
    assert "execute_sql" not in tool_names


def test_slow_response_fallback_plan_uses_core_evidence() -> None:
    steps = build_fallback_plan(
        input_text="checkout-service ????P95 ????? timeout",
        incident={
            "service_name": "checkout-service",
            "symptom": "??? P95 timeout?????????",
        },
    )

    tool_names = [step.tool_name for step in steps]

    assert "query_metrics" in tool_names
    assert "query_logs" in tool_names
    assert "query_deploy_history" in tool_names


def test_topology_prioritizes_mysql_for_order_service_sql_timeout() -> None:
    steps = build_fallback_plan(
        input_text="order-service SQL timeout，接口响应慢",
        incident={
            "service_name": "order-service",
            "symptom": "SQL timeout and latency spike",
        },
    )

    assert steps[1].tool_name == "query_service_context"
    assert steps[2].tool_name == "query_mysql_status"
    assert "服务拓扑" in steps[2].purpose


def test_raw_alert_requested_action_is_prioritized_for_risk_control() -> None:
    incident = {
        "service_name": "order-service",
        "environment": "prod",
        "symptom": "operator asks agent to run unaudited SQL",
        "raw_alert": {
            "requested_action": "execute_sql",
            "sql": "DELETE FROM orders WHERE created_at < NOW() - INTERVAL 30 DAY",
            "audited": False,
        },
    }

    steps = build_fallback_plan(
        input_text="order-service forbidden unaudited SQL",
        incident=incident,
    )
    requested_action_step = steps[0]
    decision = assess_plan_step(requested_action_step, incident=incident)

    assert requested_action_step.tool_name == "execute_sql"
    assert requested_action_step.input_args["sql"].startswith("DELETE FROM orders")
    assert requested_action_step.risk_level == "high"
    assert decision.policy == "forbidden"
    assert "sql:unaudited" in decision.matched_rules


def test_redis_demo_plan_keeps_evidence_order_and_approval_action() -> None:
    incident = {
        "service_name": "order-service",
        "environment": "prod",
        "symptom": "Redis connection timeout and 5xx spike",
        "raw_alert": {
            "alertname": "RedisMaxClientsNearLimit",
            "dependency": "redis-order",
            "requested_action": "apply_config_change",
            "reason": "调整 Redis maxclients",
        },
    }

    steps = build_fallback_plan("order-service Redis maxclients exhausted", incident)
    tool_names = [step.tool_name for step in steps]

    assert tool_names[:5] == [
        "query_redis_status",
        "query_metrics",
        "query_logs",
        "search_runbook",
        "search_history_ticket",
    ]
    assert tool_names[-1] == "apply_config_change"
    assert assess_plan_step(steps[-1], incident=incident).policy == "approval_required"


def test_requested_action_stays_inside_eight_step_execution_budget() -> None:
    incident = {
        "service_name": "catalog-service",
        "environment": "prod",
        "symptom": "Redis connection exhaustion requires operator review",
        "raw_alert": {
            "requested_action": "restart_service",
            "reason": "operator requested a production restart",
        },
    }

    steps = build_fallback_plan("catalog-service Redis maxclients exhausted", incident)

    assert steps[0].tool_name == "restart_service"
    assert len(steps) <= 8
    assert assess_plan_step(steps[0], incident=incident).policy == "approval_required"


def test_normalize_plan_steps_resets_status_and_renders_legacy_plan() -> None:
    steps = normalize_plan_steps(
        raw_steps=[
            {
                "step_id": "s1",
                "tool_name": "query_logs",
                "purpose": "检索 ERROR 日志",
                "input_args": {"service_name": "order-service"},
                "expected_evidence": "ERROR 日志证据",
                "risk_level": "low",
                "status": "success",
            },
            "人工整理最终结论",
        ],
        input_text="order-service timeout",
        incident={"service_name": "order-service"},
    )

    assert steps[0].status == "pending"
    assert steps[1].tool_name == "manual_analysis"
    assert "query_logs" in render_plan_step(steps[0])


def test_normalize_plan_steps_deduplicates_and_rejects_unavailable_tools() -> None:
    steps = normalize_plan_steps(
        raw_steps=[
            {
                "step_id": "s1",
                "tool_name": "query_metrics",
                "purpose": "检查指标",
                "input_args": {"service_name": "order-service"},
                "expected_evidence": "指标证据",
            },
            {
                "step_id": "duplicate-id",
                "tool_name": "query_metrics",
                "purpose": "重复检查同一指标",
                "input_args": {"service_name": "order-service"},
                "expected_evidence": "重复指标证据",
            },
            {
                "step_id": "unknown",
                "tool_name": "query_unavailable_backend",
                "purpose": "调用不可用工具",
                "input_args": {"service_name": "order-service"},
                "expected_evidence": "不可用工具证据",
            },
        ],
        input_text="order-service timeout",
        incident={"service_name": "order-service"},
        allowed_tool_names={"query_metrics"},
    )

    assert [(step.step_id, step.tool_name) for step in steps] == [("s1", "query_metrics")]


def test_normalize_plan_steps_uses_fallback_when_all_structured_items_are_invalid() -> None:
    steps = normalize_plan_steps(
        raw_steps=[
            {
                "step_id": "broken",
                "tool_name": "query_metrics",
                "purpose": "x" * 1001,
            }
        ],
        input_text="order-service timeout",
        incident={"service_name": "order-service"},
        allowed_tool_names={"query_metrics"},
    )

    assert steps
    assert {step.tool_name for step in steps} == {"query_metrics"}
    assert all(step.tool_name != "manual_analysis" for step in steps)


def test_normalize_plan_steps_respects_explicitly_empty_tool_contract() -> None:
    steps = normalize_plan_steps(
        raw_steps=[
            {
                "step_id": "s1",
                "tool_name": "query_metrics",
                "purpose": "检查指标",
                "input_args": {"service_name": "order-service"},
                "expected_evidence": "指标证据",
            }
        ],
        input_text="order-service timeout",
        incident={"service_name": "order-service"},
        allowed_tool_names=set(),
    )

    assert len(steps) == 1
    assert steps[0].tool_name == "manual_analysis"


def test_invalid_model_plan_keeps_requested_action_for_risk_control() -> None:
    incident = {
        "service_name": "order-service",
        "environment": "prod",
        "raw_alert": {
            "requested_action": "restart_service",
            "reason": "operator requested restart",
        },
    }

    steps = normalize_plan_steps(
        raw_steps=[{"tool_name": "query_metrics", "purpose": "x" * 1001}],
        input_text="order-service timeout",
        incident=incident,
        allowed_tool_names=set(),
    )

    assert [step.tool_name for step in steps] == ["restart_service"]
    assert assess_plan_step(steps[0], incident=incident).policy == "approval_required"


def test_ensure_unique_step_ids_handles_generated_id_collisions() -> None:
    steps = ensure_unique_step_ids(
        [
            PlanStep(step_id="s2", tool_name="query_metrics"),
            PlanStep(step_id="s2", tool_name="query_logs"),
            PlanStep(step_id="s3", tool_name="query_redis_status"),
        ]
    )

    step_ids = [step.step_id for step in steps]
    assert len(step_ids) == len(set(step_ids))
