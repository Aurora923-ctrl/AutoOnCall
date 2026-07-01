"""Tests for AIOps risk policy decisions."""

from app.agent.aiops.risk_controller import assess_plan_step
from app.models.plan import PlanStep


def test_read_only_low_risk_query_is_auto_executable() -> None:
    decision = assess_plan_step(
        PlanStep(
            step_id="s1",
            tool_name="query_metrics",
            purpose="查询服务指标",
            input_args={"service_name": "order-service"},
            risk_level="low",
        )
    )

    assert decision.policy == "allow"
    assert decision.allowed is True
    assert decision.need_approval is False
    assert decision.forbidden is False


def test_medium_remediation_suggestion_is_read_only_advice() -> None:
    decision = assess_plan_step(
        PlanStep(
            step_id="s2",
            tool_name="suggest_remediation",
            purpose="生成修复建议，包含临时限流和配置调整",
            input_args={"service_name": "order-service"},
            risk_level="medium",
        )
    )

    assert decision.policy == "allow"
    assert decision.risk_level == "medium"
    assert decision.read_only is True
    assert decision.need_approval is False
    assert decision.matched_rules == []


def test_high_remediation_suggestion_still_does_not_create_approval() -> None:
    decision = assess_plan_step(
        PlanStep(
            step_id="s2-high",
            tool_name="suggest_remediation",
            purpose="生成高风险修复建议，提醒真实重启服务动作需审批",
            input_args={"service_name": "order-service"},
            risk_level="high",
        ),
        incident={"environment": "prod", "service_name": "order-service"},
    )

    assert decision.policy == "allow"
    assert decision.risk_level == "high"
    assert decision.read_only is True
    assert decision.need_approval is False
    assert decision.matched_rules == []
    assert "不创建审批单" in decision.reason


def test_production_restart_service_is_high_risk_approval() -> None:
    decision = assess_plan_step(
        PlanStep(
            step_id="s3",
            tool_name="restart_service",
            purpose="重启生产服务以释放异常连接",
            input_args={"service_name": "order-service"},
            risk_level="medium",
        ),
        incident={"environment": "prod", "service_name": "order-service"},
    )

    assert decision.policy == "approval_required"
    assert decision.risk_level == "high"
    assert decision.need_approval is True
    assert "tool:not-read-only" in decision.matched_rules


def test_read_only_pod_restart_count_query_is_not_approval_action() -> None:
    decision = assess_plan_step(
        PlanStep(
            step_id="s3-readonly",
            tool_name="query_k8s_status",
            purpose="检查 Pod restart count 和 CrashLoopBackOff 状态",
            input_args={"service_name": "inventory-service"},
            risk_level="low",
        ),
        incident={
            "environment": "prod",
            "service_name": "inventory-service",
            "symptom": "Pod CrashLoopBackOff and restart count increasing",
        },
    )

    assert decision.policy == "allow"
    assert decision.need_approval is False


def test_delete_pod_is_forbidden_by_default() -> None:
    decision = assess_plan_step(
        PlanStep(
            step_id="s4",
            tool_name="delete_pod",
            purpose="删除 Pod 以触发重建",
            input_args={"pod_name": "order-service-abc"},
            risk_level="high",
        )
    )

    assert decision.policy == "forbidden"
    assert decision.allowed is False
    assert decision.forbidden is True
    assert decision.risk_level == "high"
    assert "tool:delete_pod" in decision.matched_rules


def test_dangerous_shell_command_is_forbidden() -> None:
    decision = assess_plan_step(
        PlanStep(
            step_id="s5",
            tool_name="run_shell",
            purpose="清理目录",
            input_args={"command": "rm -rf /data/orders"},
            risk_level="high",
        )
    )

    assert decision.policy == "forbidden"
    assert decision.forbidden is True
    assert "shell:rm-rf" in decision.matched_rules


def test_unaudited_write_sql_is_forbidden() -> None:
    decision = assess_plan_step(
        PlanStep(
            step_id="s6",
            tool_name="execute_sql",
            purpose="执行未审核 SQL",
            input_args={"sql": "delete from orders where created_at < now()"},
            risk_level="high",
        )
    )

    assert decision.policy == "forbidden"
    assert decision.forbidden is True
    assert "sql:unaudited" in decision.matched_rules
