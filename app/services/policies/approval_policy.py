"""Canonical approval lifecycle and risky-action classification rules."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

RiskPolicy = Literal["allow", "approval_required", "forbidden"]
RISK_POLICY_VERSION = "2026-07-21.1"

APPROVAL_PENDING_STATUS = "pending"
APPROVAL_TERMINAL_STATUSES = frozenset({"approved", "rejected", "cancelled"})
APPROVAL_DECISION_STATUS = {
    "approve": "approved",
    "reject": "rejected",
    "cancel": "cancelled",
}
APPROVAL_INCIDENT_STATUS = {
    "pending": "waiting_approval",
    "approved": "approval_approved",
    "rejected": "approval_rejected",
    "cancelled": "approval_cancelled",
}

READ_ONLY_PREFIXES: tuple[str, ...] = ()
READ_ONLY_TOOL_NAMES = frozenset(
    {
        "manual_analysis",
        "suggest_remediation",
        "query_metrics",
        "query_logs",
        "query_service_context",
        "query_deploy_history",
        "query_redis_status",
        "query_k8s_status",
        "query_mysql_status",
        "search_runbook",
        "search_history_ticket",
    }
)
HARD_FORBIDDEN_TOOLS = frozenset(
    {
        "delete_pod",
        "delete_k8s_pod",
        "restart_database",
        "modify_prod_config",
        "execute_shell",
        "run_shell",
        "run_command",
        "execute_command",
        "execute_sql",
        "run_sql",
        "kill_process",
        "shutdown_host",
    }
)
ACTION_TOOLS_REQUIRING_APPROVAL = frozenset(
    {
        "restart_service",
        "scale_service",
        "rollback_deployment",
        "apply_config_change",
        "drain_node",
        "clear_cache",
    }
)

FORBIDDEN_PATTERNS = (
    (re.compile(r"\brm\s*[-\u2010-\u2015]\s*(?:r\s*f|f\s*r)\b", re.IGNORECASE), "shell:rm-rf"),
    (re.compile(r"\bkill\s*[-\u2010-\u2015]\s*9\b", re.IGNORECASE), "shell:kill-9"),
    (re.compile(r"\bshutdown\b", re.IGNORECASE), "shell:shutdown"),
    (re.compile(r"\b(?:kubectl|k)\s+(?:delete|del|rm)\b", re.IGNORECASE), "k8s:delete"),
    (
        re.compile(r"\bredis-cli\b.*\bflush\s*(?:all|db)\b|\bredis-cli\b.*\bflush(?:all|db)\b", re.IGNORECASE),
        "redis:flush",
    ),
    (re.compile(r"\bconfig\s+set\b", re.IGNORECASE), "redis:config-set"),
    (re.compile(r"\bdrop\s*table\b", re.IGNORECASE), "sql:drop-table"),
    (re.compile(r"\btruncate\s*table\b", re.IGNORECASE), "sql:truncate-table"),
    (re.compile(r"\bdelete\s*from\b", re.IGNORECASE), "sql:delete"),
    (re.compile(r"\bupdate\s+\w+", re.IGNORECASE), "sql:update"),
    (re.compile(r"\binsert\s*into\b", re.IGNORECASE), "sql:insert"),
    (re.compile(r"\balter\s*table\b", re.IGNORECASE), "sql:alter-table"),
    (re.compile(r"\u5220\u9664\s*pod", re.IGNORECASE), "k8s:delete-pod"),
    (re.compile(r"\u91cd\u542f.*\u6570\u636e\u5e93", re.IGNORECASE), "database:restart"),
    (
        re.compile(r"\u4fee\u6539.*\u751f\u4ea7.*\u914d\u7f6e", re.IGNORECASE),
        "config:modify-prod",
    ),
    (re.compile(r"\u672a\u5ba1\u6838.*sql", re.IGNORECASE), "sql:unaudited"),
)

APPROVAL_PATTERNS = (
    (
        re.compile(r"\brestart\s+(service|pod|database|db|deployment)\b", re.IGNORECASE),
        "action:restart",
    ),
    (re.compile(r"\brollback\s+(service|deployment|release)\b", re.IGNORECASE), "action:rollback"),
    (
        re.compile(r"\bscale\s+(service|deployment|replica|replicas)\b", re.IGNORECASE),
        "action:scale",
    ),
    (re.compile(r"\blimit\s+traffic\b", re.IGNORECASE), "action:limit-traffic"),
    (re.compile(r"\u91cd\u542f.*\u670d\u52a1", re.IGNORECASE), "action:restart-service"),
    (re.compile(r"\u6269\u5bb9|\u7f29\u5bb9", re.IGNORECASE), "action:scale"),
    (re.compile(r"\u56de\u6eda", re.IGNORECASE), "action:rollback"),
    (re.compile(r"\u9650\u6d41|\u964d\u7ea7", re.IGNORECASE), "action:traffic-control"),
    (re.compile(r"\u8c03\u6574.*maxclients", re.IGNORECASE), "action:redis-config"),
    (re.compile(r"\u4fee\u6539.*\u914d\u7f6e", re.IGNORECASE), "action:config-change"),
)


def incident_status_from_approvals(statuses: Iterable[str]) -> str:
    """Infer an Incident status from approval records only."""
    normalized = tuple(statuses)
    for status in ("pending", "approved", "rejected", "cancelled"):
        if status in normalized:
            return APPROVAL_INCIDENT_STATUS[status]
    return "approval_decided" if normalized else "investigating"


def effective_approval_status(statuses: Iterable[str], *, latest_status: str = "") -> str:
    """Return the effective approval status, prioritizing active requests."""
    normalized = tuple(statuses)
    if "pending" in normalized:
        return "pending"
    if normalized:
        return latest_status or normalized[-1]
    return "not_required"


def matched_action_patterns(
    context: str,
    *,
    forbidden: bool,
) -> list[str]:
    """Return stable rule names matched by action-owned text."""
    patterns = FORBIDDEN_PATTERNS if forbidden else APPROVAL_PATTERNS
    return [rule_name for pattern, rule_name in patterns if pattern.search(context)]
