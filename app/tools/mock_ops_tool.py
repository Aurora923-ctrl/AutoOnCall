"""Operational tools with optional real adapters and deterministic mock fallback."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.integrations.kubernetes import KubernetesStatusAdapter
from app.integrations.mysql import MySQLStatusAdapter
from app.integrations.ticketing import TicketingAdapter
from app.services.service_topology import get_primary_dependency_instance
from app.tools.base import AIOpsTool, clamp_duration, clamp_int
from app.tools.fallback import run_adapter_or_mock


class QueryK8sStatusTool(AIOpsTool):
    name = "query_k8s_status"
    description = "Query pod status, restarts, image versions, and deployment timing."
    risk_level = "low"
    read_only = True
    data_sources = ["Kubernetes API", "mock"]
    degradation_strategy = (
        "Use Kubernetes API when configured; otherwise return mock pod status when enabled "
        "or a structured unavailable payload when mock fallback is disabled."
    )

    def __init__(self, k8s_adapter: KubernetesStatusAdapter | None = None):
        super().__init__()
        self._allow_adapter_failure_fallback = k8s_adapter is None
        self._k8s_adapter = k8s_adapter or KubernetesStatusAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        time_range = clamp_duration(
            input_args.get("time_range"),
            default="10m",
            maximum_seconds=3600,
        )
        input_args["time_range"] = time_range
        return await run_adapter_or_mock(
            configured=self._k8s_adapter.configured,
            adapter_call=lambda: self._k8s_adapter.query_service_status(
                service_name, time_range
            ),
            mock_call=lambda: self._mock_status(service_name, time_range),
            source="kubernetes",
            required_config="KUBERNETES_API_SERVER",
            failure_summary_prefix="Kubernetes query failed",
            not_configured_summary_prefix="Kubernetes status query unavailable",
            payload={"service_name": service_name, "time_range": time_range},
            unavailable_defaults={"pods": [], "events": []},
            allow_failure_fallback=self._allow_adapter_failure_fallback,
        )

    @staticmethod
    def _mock_status(service_name: str, time_range: str) -> dict[str, Any]:
        now = datetime.now()
        recent_image_tag = now.strftime("%Y.%m.%d")
        if service_name == "inventory-service":
            return {
                "service_name": service_name,
                "time_range": time_range,
                "source": "mock",
                "pods": [
                    {
                        "name": f"{service_name}-7f8d9c-abc12",
                        "ready": False,
                        "restarts": 12,
                        "status": "CrashLoopBackOff",
                        "last_state": "OOMKilled",
                    },
                    {
                        "name": f"{service_name}-7f8d9c-def34",
                        "ready": True,
                        "restarts": 1,
                        "status": "Running",
                    },
                ],
                "events": [
                    {
                        "reason": "BackOff",
                        "message": (
                            "Back-off restarting failed container "
                            f"{service_name} in pod {service_name}-7f8d9c-abc12"
                        ),
                        "count": 12,
                    },
                    {
                        "reason": "OOMKilled",
                        "message": "Container terminated due to memory limit",
                        "count": 3,
                    },
                ],
                "deployment": {
                    "image": f"{service_name}:{recent_image_tag}",
                    "updated_at": (now - timedelta(minutes=18)).strftime("%Y-%m-%d %H:%M:%S"),
                },
                "summary": "Pod is in CrashLoopBackOff; primary instance restarted 12 times and was OOMKilled",
            }
        return {
            "service_name": service_name,
            "time_range": time_range,
            "source": "mock",
            "pods": [
                {
                    "name": f"{service_name}-7f8d9c-abc12",
                    "ready": True,
                    "restarts": 0,
                    "status": "Running",
                },
                {
                    "name": f"{service_name}-7f8d9c-def34",
                    "ready": True,
                    "restarts": 1,
                    "status": "Running",
                },
            ],
            "events": [],
            "deployment": {
                "image": f"{service_name}:{recent_image_tag}",
                "updated_at": (now - timedelta(minutes=42)).strftime("%Y-%m-%d %H:%M:%S"),
            },
            "summary": "Pods are running and no widespread CrashLoopBackOff was found",
        }


class QueryMySQLStatusTool(AIOpsTool):
    name = "query_mysql_status"
    description = "Query MySQL slow SQL, connection pool, lock waits, and active connections."
    risk_level = "low"
    read_only = True
    data_sources = ["MySQL status SQL", "service topology", "mock"]
    degradation_strategy = (
        "Use MySQL adapters when configured; otherwise return mock SQL evidence when enabled "
        "or a structured unavailable payload when mock fallback is disabled."
    )

    def __init__(self, mysql_adapter: MySQLStatusAdapter | None = None):
        super().__init__()
        self._allow_adapter_failure_fallback = mysql_adapter is None
        self._mysql_adapter = mysql_adapter or MySQLStatusAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        mysql_instance = (
            input_args.get("mysql_instance")
            or get_primary_dependency_instance(service_name, "mysql")
            or ""
        )
        return await run_adapter_or_mock(
            configured=self._mysql_adapter.configured,
            adapter_call=lambda: self._mysql_adapter.query_status(
                service_name, mysql_instance
            ),
            mock_call=lambda: self._mock_status(service_name, mysql_instance),
            source="mysql",
            required_config="MYSQL_DSN",
            failure_summary_prefix="MySQL query failed",
            not_configured_summary_prefix="MySQL status query unavailable",
            payload={"service_name": service_name, "mysql_instance": mysql_instance},
            unavailable_defaults={"slow_queries": [], "connections": {}, "lock_waits": 0},
            allow_failure_fallback=self._allow_adapter_failure_fallback,
        )

    @staticmethod
    def _mock_status(service_name: str, mysql_instance: str) -> dict[str, Any]:
        return {
            "service_name": service_name,
            "mysql_instance": mysql_instance,
            "source": "mock",
            "slow_queries": [
                {"sql_digest": "select * from orders where user_id=?", "avg_ms": 420, "count": 18}
            ],
            "connections": {"active": 84, "max": 200, "pool_waiting": 3},
            "lock_waits": 0,
            "summary": "MySQL has a small number of slow queries, but the connection pool is not exhausted",
        }


class SearchHistoryTicketTool(AIOpsTool):
    name = "search_history_ticket"
    description = "Search historical tickets for similar incidents."
    risk_level = "low"
    read_only = True
    data_sources = ["ticketing API", "mock"]
    degradation_strategy = (
        "Use ticketing API when configured; otherwise return mock historical tickets when "
        "enabled or a structured unavailable payload when mock fallback is disabled."
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
        return await run_adapter_or_mock(
            configured=self._ticketing_adapter.configured,
            adapter_call=lambda: self._ticketing_adapter.search_history(
                service_name, query, limit
            ),
            mock_call=lambda: self._mock_history(service_name),
            source="ticket_api",
            required_config="TICKET_API_URL",
            failure_summary_prefix="Ticket history query failed",
            not_configured_summary_prefix="Ticket history query unavailable",
            payload={"service_name": service_name, "query": query, "limit": limit},
            unavailable_defaults={"tickets": []},
            allow_failure_fallback=self._allow_adapter_failure_fallback,
        )

    @staticmethod
    def _mock_history(service_name: str) -> dict[str, Any]:
        return {
            "service_name": service_name,
            "source": "mock",
            "tickets": [
                {
                    "ticket_id": "INC-2026-0618-REDIS-001",
                    "title": "order-service Redis maxclients exhausted",
                    "root_cause": "Redis connections reached maxclients and application pools did not release connections in time",
                    "resolution": "Release abnormal connections, adjust pool limits, and add maxclients monitoring",
                }
            ],
            "summary": "Found 1 similar Redis maxclients incident",
        }


class SuggestRemediationTool(AIOpsTool):
    name = "suggest_remediation"
    description = "Generate remediation suggestions from evidence without executing risky actions."
    risk_level = "medium"
    read_only = True
    data_sources = ["diagnosis evidence", "rule based policy"]
    degradation_strategy = (
        "Generate suggestions only; changes must go through approval and safe-change workflow."
    )

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
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
