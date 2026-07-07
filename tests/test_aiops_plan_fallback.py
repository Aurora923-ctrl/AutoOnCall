"""Tests for deterministic structured AIOps planning fallback."""

from app.agent.aiops.plan_fallback import (
    build_fallback_plan,
    normalize_plan_steps,
    render_plan_step,
)
from app.agent.aiops.risk_controller import assess_plan_step


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
