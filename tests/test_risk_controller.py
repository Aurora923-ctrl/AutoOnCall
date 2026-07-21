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


def test_qualified_production_environment_keeps_write_action_high_risk() -> None:
    for environment in ("prod-cn", "production-us", "prd_east"):
        decision = assess_plan_step(
            PlanStep(
                step_id=f"restart-{environment}",
                tool_name="restart_service",
                purpose="Restart the affected service",
                input_args={"service_name": "order-service"},
                risk_level="medium",
            ),
            incident={"environment": environment, "service_name": "order-service"},
        )

        assert decision.policy == "approval_required"
        assert decision.risk_level == "high"


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


def test_read_only_query_is_not_blocked_by_incident_action_text() -> None:
    decision = assess_plan_step(
        PlanStep(
            step_id="s-context",
            tool_name="query_metrics",
            purpose="检查服务指标",
            input_args={"service_name": "order-service"},
            expected_evidence="确认延迟和错误率",
            risk_level="low",
        ),
        incident={
            "environment": "prod",
            "service_name": "order-service",
            "symptom": "operator requested restart service after diagnosis",
        },
    )

    assert decision.policy == "allow"
    assert decision.need_approval is False


def test_action_pattern_inside_step_input_still_requires_approval() -> None:
    decision = assess_plan_step(
        PlanStep(
            step_id="s-action-input",
            tool_name="custom_action",
            purpose="执行操作员请求",
            input_args={"requested_action": "restart service"},
            expected_evidence="动作执行结果",
            risk_level="medium",
        ),
        incident={"environment": "prod", "service_name": "order-service"},
    )

    assert decision.policy == "approval_required"
    assert "action:restart" in decision.matched_rules


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


def test_dangerous_patterns_are_normalized_before_matching() -> None:
    cases = [
        ("DROP/**/TABLE orders", "sql:drop-table"),
        ("redis-cli FLUSH ALL", "redis:flush"),
        ("kill - 9 123", "shell:kill-9"),
    ]

    for command, expected_rule in cases:
        decision = assess_plan_step(
            PlanStep(
                tool_name="query_mysql_status",
                purpose="Validate operator-provided text",
                input_args={"query": command},
                risk_level="low",
            )
        )

        assert decision.policy == "forbidden"
        assert expected_rule in decision.matched_rules


def test_untrusted_suggest_remediation_cannot_use_read_only_exception() -> None:
    class UntrustedRegistry:
        def get_policy_metadata(self, name: str) -> dict:
            return {"name": name, "read_only": False, "risk_level": "high", "trusted": False}

    decision = assess_plan_step(
        PlanStep(
            tool_name="suggest_remediation",
            purpose="Untrusted extension action",
            input_args={},
            risk_level="low",
        ),
        tool_registry=UntrustedRegistry(),
    )

    assert decision.policy == "approval_required"
    assert decision.risk_level == "high"
    assert decision.read_only is False


def test_registry_missing_tool_is_high_risk_and_not_read_only() -> None:
    class Registry:
        def get_policy_metadata(self, name: str):
            return None

    decision = assess_plan_step(
        PlanStep(
            tool_name="unknown_extension",
            purpose="Run an extension action",
            input_args={},
            risk_level="low",
        ),
        tool_registry=Registry(),
    )

    assert decision.policy == "approval_required"
    assert decision.risk_level == "high"
    assert decision.read_only is False


def test_caller_supplied_audited_flag_cannot_allow_write_sql() -> None:
    decision = assess_plan_step(
        PlanStep(
            tool_name="query_mysql_status",
            purpose="Inspect a caller-provided query",
            input_args={"sql": "UPDATE orders SET status = 'done'", "audited": True},
            risk_level="low",
        )
    )

    assert decision.policy == "forbidden"
    assert "sql:unaudited" in decision.matched_rules


def test_common_command_aliases_are_forbidden() -> None:
    for command, rule in (
        ("k rm pod order-1", "k8s:delete"),
        ("redis-cli FLUSHALL", "redis:flush"),
        ("rm -fr /data/orders", "shell:rm-rf"),
    ):
        decision = assess_plan_step(
            PlanStep(
                tool_name="query_logs",
                purpose="Inspect text",
                input_args={"text": command},
                risk_level="low",
            )
        )
        assert decision.policy == "forbidden"
        assert rule in decision.matched_rules


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
