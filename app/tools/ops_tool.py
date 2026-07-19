"""Operational tools backed by external adapters."""

from __future__ import annotations

from typing import Any

from app.integrations.base import adapter_failure, adapter_not_configured
from app.integrations.kubernetes import KubernetesStatusAdapter
from app.integrations.mysql import MySQLStatusAdapter
from app.integrations.ticketing import TicketingAdapter
from app.services.service_topology import get_primary_dependency_instance
from app.tools.base import AIOpsTool, ToolRetryPolicy, clamp_duration, clamp_int


class QueryK8sStatusTool(AIOpsTool):
    name = "query_k8s_status"
    description = "Query pod status, restarts, image versions, and deployment timing."
    input_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string", "minLength": 1},
            "time_range": {"type": "string", "default": "10m"},
        },
        "required": ["service_name"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    retry_policy = ToolRetryPolicy(
        max_attempts=2,
        backoff_seconds=0.1,
        retry_on=["timeout", "connection_error", "server_error"],
    )
    data_sources = ["Kubernetes API"]
    degradation_strategy = (
        "Use Kubernetes API when configured; otherwise return a structured unavailable payload."
    )

    def __init__(self, k8s_adapter: KubernetesStatusAdapter | None = None):
        super().__init__()
        self._k8s_adapter = k8s_adapter or KubernetesStatusAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        time_range = clamp_duration(
            input_args.get("time_range"),
            default="10m",
            maximum_seconds=3600,
        )
        input_args["time_range"] = time_range
        payload = {"service_name": service_name, "time_range": time_range}
        if not self._k8s_adapter.configured:
            unavailable = adapter_not_configured(
                "kubernetes",
                required_config="KUBERNETES_API_SERVER",
                summary_prefix="Kubernetes status query unavailable",
                **payload,
            )
            unavailable.update({"pods": [], "events": []})
            return unavailable
        try:
            return await self._k8s_adapter.query_service_status(service_name, time_range)
        except Exception as exc:
            failure = adapter_failure(
                "kubernetes",
                exc,
                summary_prefix="Kubernetes query failed",
                **payload,
            )
            failure.update({"pods": [], "events": []})
            return failure


class QueryMySQLStatusTool(AIOpsTool):
    name = "query_mysql_status"
    description = "Query MySQL slow SQL, connection pool, lock waits, and active connections."
    input_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string", "minLength": 1},
            "mysql_instance": {"type": "string"},
        },
        "required": ["service_name"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    retry_policy = ToolRetryPolicy(
        max_attempts=2,
        backoff_seconds=0.1,
        retry_on=["timeout", "connection_error", "server_error"],
    )
    data_sources = ["MySQL status SQL", "MySQL live incident evidence tables", "service topology"]
    degradation_strategy = (
        "Use MySQL adapters when configured; otherwise return a structured unavailable payload."
    )

    def __init__(self, mysql_adapter: MySQLStatusAdapter | None = None):
        super().__init__()
        self._mysql_adapter = mysql_adapter or MySQLStatusAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        mysql_instance = (
            input_args.get("mysql_instance")
            or get_primary_dependency_instance(service_name, "mysql")
            or ""
        )
        payload = {"service_name": service_name, "mysql_instance": mysql_instance}
        if not self._mysql_adapter.configured:
            unavailable = adapter_not_configured(
                "mysql",
                required_config="MYSQL_DSN, MYSQL_URL, MYSQL_INSTANCES, or MYSQL_HOST",
                summary_prefix="MySQL status query unavailable",
                **payload,
            )
            unavailable.update({"slow_queries": [], "connections": {}, "lock_waits": 0})
            return unavailable
        try:
            return await self._mysql_adapter.query_status(service_name, mysql_instance)
        except Exception as exc:
            failure = adapter_failure(
                "mysql",
                exc,
                summary_prefix="MySQL query failed",
                **payload,
            )
            failure.update({"slow_queries": [], "connections": {}, "lock_waits": 0})
            return failure


class SearchHistoryTicketTool(AIOpsTool):
    name = "search_history_ticket"
    description = "Search historical tickets for similar incidents."
    input_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string", "minLength": 1},
            "query": {"type": "string"},
            "symptom": {"type": "string"},
            "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
        },
        "required": ["service_name"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    retry_policy = ToolRetryPolicy(
        max_attempts=2,
        backoff_seconds=0.1,
        retry_on=["timeout", "connection_error", "server_error"],
    )
    data_sources = ["ticketing API", "MySQL historical tickets"]
    degradation_strategy = (
        "Use the configured ticketing API or MySQL-backed historical tickets; return a "
        "structured unavailable payload when neither real source is configured."
    )

    def __init__(self, ticketing_adapter: TicketingAdapter | None = None):
        super().__init__()
        self._allow_adapter_failure_fallback = ticketing_adapter is None
        self._ticketing_adapter = ticketing_adapter or TicketingAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        query = input_args.get("query") or input_args.get("symptom") or ""
        limit = clamp_int(input_args.get("limit"), default=5, minimum=1, maximum=20)
        input_args["limit"] = limit
        if self._ticketing_adapter.configured:
            try:
                return await self._ticketing_adapter.search_history(service_name, query, limit)
            except Exception as exc:
                failure = adapter_failure(
                    "ticket_api",
                    exc,
                    summary_prefix="Ticket history query failed",
                    service_name=service_name,
                    query=query,
                    limit=limit,
                )
                failure.update({"tickets": []})
                return failure

        payload = adapter_not_configured(
            "ticket_api",
            required_config="TICKET_API_URL or MYSQL_DSN",
            summary_prefix="Ticket history query unavailable",
            service_name=service_name,
            query=query,
            limit=limit,
        )
        payload.update({"tickets": []})
        return payload


class SuggestRemediationTool(AIOpsTool):
    name = "suggest_remediation"
    description = "Generate remediation suggestions from evidence without executing risky actions."
    input_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string", "minLength": 1},
            "symptom": {"type": "string"},
            "query": {"type": "string"},
        },
        "required": ["service_name"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    risk_level = "medium"
    read_only = True
    data_sources = ["diagnosis evidence", "rule based policy"]
    degradation_strategy = (
        "Generate suggestions only; changes must go through approval and safe-change workflow."
    )

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        symptom = str(input_args.get("symptom") or input_args.get("query") or "").lower()
        context = f"{service_name} {symptom}"
        if any(token in context for token in ["mysql", "sql", "slow query", "慢查询", "连接池"]):
            return {
                "service_name": service_name,
                "source": "rule_based",
                "short_term": [
                    "Keep diagnosis read-only and confirm the slow SQL digest, pool waiting, and recent release timing",
                    "Throttle or disable the expensive payment report path if it is behind a feature flag",
                    "Ask the DBA/application owner to review EXPLAIN before any index or SQL change",
                ],
                "medium_term": [
                    "Add a covering index or rewrite the report SQL after approval and staging verification",
                    "Add alerts for pool waiting, Slow_queries growth, and Threads_connected saturation",
                ],
                "risk_level": "medium",
                "change_requires_approval": True,
                "approval_scope": "sql_or_config_change_only",
                "summary": (
                    "MySQL remediation suggestions generated; SQL, index, and config changes "
                    "require human approval"
                ),
            }
        return {
            "service_name": service_name,
            "source": "rule_based",
            "short_term": [
                "Submit approval before releasing abnormal Redis connections or restarting clients",
                "Temporarily rate-limit high-traffic endpoints to reduce Redis connection pressure",
                "Inspect connection pool leaks and reduce idle connection retention",
            ],
            "medium_term": [
                "Optimize Redis connection pool configuration and timeout policy",
                "Add connected_clients/maxclients alerts",
            ],
            "risk_level": "medium",
            "change_requires_approval": True,
            "approval_scope": "suggested_change_only",
            "summary": "Remediation suggestions generated; real change actions require human approval",
        }
