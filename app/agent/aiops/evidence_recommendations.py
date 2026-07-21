"""Recommended evidence collection and retry steps."""

from __future__ import annotations

from typing import Any

from app.models.plan import PlanStep


def build_recommended_steps(missing_tools: list[str], service_name: str) -> list[PlanStep]:
    """Build read-only steps that fill known evidence gaps."""
    builders = {
        "query_metrics": lambda: PlanStep(
            step_id="replan-metrics",
            tool_name="query_metrics",
            purpose=f"补充查询 {service_name} 最近 10 分钟的 QPS、P95、错误率、CPU 和内存",
            input_args={"service_name": service_name, "time_range": "10m", "interval": "1m"},
            expected_evidence="确认服务是否存在延迟、错误率或资源异常",
            risk_level="low",
        ),
        "query_logs": lambda: PlanStep(
            step_id="replan-logs",
            tool_name="query_logs",
            purpose=f"补充查询 {service_name} 最近 10 分钟 ERROR 和 timeout 日志",
            input_args={
                "service_name": service_name,
                "time_range": "10m",
                "query": "ERROR OR timeout",
            },
            expected_evidence="确认是否存在 timeout、5xx 或下游依赖异常日志",
            risk_level="low",
        ),
        "query_redis_status": lambda: PlanStep(
            step_id="replan-redis",
            tool_name="query_redis_status",
            purpose="补充查询 Redis connected_clients、maxclients、blocked_clients 和慢日志",
            input_args={"service_name": service_name, "time_range": "10m"},
            expected_evidence="判断 Redis 是否存在连接数耗尽或慢命令异常",
            risk_level="low",
        ),
        "search_runbook": lambda: PlanStep(
            step_id="replan-runbook",
            tool_name="search_runbook",
            purpose="检索 Redis maxclients 与 connection timeout 的处置依据",
            input_args={
                "query": f"{service_name} Redis maxclients connected_clients timeout runbook"
            },
            expected_evidence="Runbook 仅提供诊断与处置依据，不作为当前运行态事实",
            risk_level="low",
        ),
        "search_history_ticket": lambda: PlanStep(
            step_id="replan-history",
            tool_name="search_history_ticket",
            purpose="检索历史 Redis maxclients 相似故障工单",
            input_args={
                "service_name": service_name,
                "query": "Redis maxclients connection timeout",
                "limit": 5,
            },
            expected_evidence="历史工单提供相似根因和恢复经验，但不替代当前事故证据",
            risk_level="low",
        ),
        "query_mysql_status": lambda: PlanStep(
            step_id="replan-mysql",
            tool_name="query_mysql_status",
            purpose=f"补充查询 {service_name} 相关 MySQL 慢查询、连接池和锁等待",
            input_args={"service_name": service_name, "time_range": "10m"},
            expected_evidence="判断 MySQL 是否存在慢查询、连接池耗尽或锁等待",
            risk_level="low",
        ),
        "query_deploy_history": lambda: PlanStep(
            step_id="replan-deploy-history",
            tool_name="query_deploy_history",
            purpose=f"补充查询 {service_name} 最近发布和 Feature Flag 变更",
            input_args={"service_name": service_name, "time_range": "24h", "limit": 5},
            expected_evidence="发布时间用于提高或降低变更相关假设排序，但不能单独证明根因",
            risk_level="low",
        ),
        "query_k8s_status": lambda: PlanStep(
            step_id="replan-k8s",
            tool_name="query_k8s_status",
            purpose=f"补充查询 {service_name} Pod 状态、重启次数和部署版本",
            input_args={"service_name": service_name, "time_range": "10m"},
            expected_evidence="判断 Pod 是否 CrashLoopBackOff、频繁重启或版本异常",
            risk_level="low",
        ),
    }
    return [builders[tool_name]() for tool_name in missing_tools if tool_name in builders]


def build_retry_steps(failed_records: list[dict[str, Any]]) -> list[PlanStep]:
    """Build at most one explicit retry for a failed diagnostic tool."""
    retry_steps: list[PlanStep] = []
    for record in failed_records:
        tool_name = str(record.get("tool_name") or "")
        if not tool_name or tool_name == "manual_analysis":
            continue
        step_id = str(record.get("step_id") or "failed-step")
        retry_steps.append(
            PlanStep(
                step_id=f"{step_id}-retry",
                tool_name=tool_name,
                purpose=f"重试失败的工具调用 {tool_name}",
                input_args=dict(record.get("input_args") or {}),
                expected_evidence=f"确认 {tool_name} 是否仍然失败，并补齐对应诊断证据",
                risk_level="low",
                retry_count=1,
            )
        )
    return retry_steps[:1]
