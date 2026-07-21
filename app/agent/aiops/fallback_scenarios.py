"""Scenario specifications for deterministic fallback planning."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FallbackScenario:
    name: str
    keywords: tuple[str, ...]
    tool_names: tuple[str, ...]


FALLBACK_SCENARIOS = (
    FallbackScenario(
        "redis",
        ("redis", "connection timeout", "maxclients", "连接超时", "连接数"),
        (
            "query_service_context",
            "query_metrics",
            "query_logs",
            "query_redis_status",
            "query_deploy_history",
            "search_runbook",
            "search_history_ticket",
            "suggest_remediation",
        ),
    ),
    FallbackScenario(
        "mysql",
        ("mysql", "slow query", "慢查询", "锁等待", "连接池"),
        (
            "query_service_context",
            "query_metrics",
            "query_logs",
            "query_mysql_status",
            "query_deploy_history",
            "search_runbook",
            "search_history_ticket",
            "suggest_remediation",
        ),
    ),
    FallbackScenario(
        "crashloop",
        ("crashloopbackoff", "crash loop", "pod crash", "pod 重启", "重启次数"),
        (
            "query_service_context",
            "query_k8s_status",
            "query_logs",
            "query_metrics",
            "query_deploy_history",
            "search_runbook",
        ),
    ),
    FallbackScenario(
        "unavailable",
        ("服务不可用", "unavailable", "5xx", "503", "502", "不可用", "无法访问"),
        (
            "query_service_context",
            "query_metrics",
            "query_logs",
            "query_k8s_status",
            "query_deploy_history",
            "search_runbook",
            "search_history_ticket",
        ),
    ),
    FallbackScenario(
        "latency",
        ("响应慢", "slow", "latency", "p95", "超时", "timeout", "延迟"),
        (
            "query_service_context",
            "query_metrics",
            "query_logs",
            "query_mysql_status",
            "query_redis_status",
            "query_deploy_history",
            "search_runbook",
        ),
    ),
    FallbackScenario(
        "cpu",
        ("cpu", "高 cpu", "cpu 高", "cpu 使用率"),
        ("query_metrics", "query_logs", "search_runbook", "suggest_remediation"),
    ),
    FallbackScenario(
        "memory",
        ("memory", "内存", "oom", "内存高"),
        ("query_metrics", "query_logs", "query_k8s_status", "search_runbook"),
    ),
    FallbackScenario(
        "disk",
        ("disk", "磁盘", "磁盘高", "磁盘满", "no space"),
        ("query_metrics", "query_logs", "query_k8s_status", "search_runbook"),
    ),
)


def match_fallback_scenario(text: str) -> FallbackScenario | None:
    """Return the first matching scenario specification."""
    lowered = text.lower()
    return next(
        (
            scenario
            for scenario in FALLBACK_SCENARIOS
            if any(keyword in lowered for keyword in scenario.keywords)
        ),
        None,
    )
