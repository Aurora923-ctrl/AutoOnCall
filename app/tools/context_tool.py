"""Service-context tools backed by CMDB and deployment-history adapters."""

from __future__ import annotations

from typing import Any

from app.integrations.base import adapter_failure, adapter_not_configured
from app.integrations.service_catalog import CMDBAdapter, DeployHistoryAdapter
from app.tools.base import AIOpsTool, ToolRetryPolicy, clamp_int


class QueryServiceContextTool(AIOpsTool):
    name = "query_service_context"
    description = "Query service owner, namespace, dependencies, and business context."
    input_schema = {
        "type": "object",
        "properties": {"service_name": {"type": "string", "minLength": 1}},
        "required": ["service_name"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    timeout_seconds = 8.0
    retry_policy = ToolRetryPolicy(
        max_attempts=2,
        backoff_seconds=0.1,
        retry_on=["timeout", "connection_error", "server_error"],
    )
    data_sources = ["CMDB API", "MySQL service catalog"]
    degradation_strategy = (
        "Use the configured CMDB API or MySQL-backed service catalog; return a structured "
        "unavailable payload when neither real source is configured."
    )

    def __init__(self, cmdb_adapter: CMDBAdapter | None = None):
        super().__init__()
        self._cmdb_adapter = cmdb_adapter or CMDBAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        if self._cmdb_adapter.configured:
            try:
                return await self._cmdb_adapter.query_service(service_name)
            except Exception as exc:
                failure = adapter_failure(
                    "cmdb",
                    exc,
                    summary_prefix="CMDB query failed",
                    service_name=service_name,
                )
                failure.update({"service": {}, "dependencies": []})
                return failure

        payload = adapter_not_configured(
            "cmdb",
            required_config="CMDB_API_URL or MYSQL_DSN",
            summary_prefix="CMDB query unavailable",
            service_name=service_name,
        )
        payload.update({"service": {}, "dependencies": []})
        return payload


class QueryDeployHistoryTool(AIOpsTool):
    name = "query_deploy_history"
    description = "Query recent service deployments, changes, and rollback records."
    input_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
        },
        "required": ["service_name"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    timeout_seconds = 8.0
    retry_policy = ToolRetryPolicy(
        max_attempts=2,
        backoff_seconds=0.1,
        retry_on=["timeout", "connection_error", "server_error"],
    )
    data_sources = ["deployment history API", "MySQL deploy history"]
    degradation_strategy = (
        "Use the configured deployment-history API or MySQL-backed deployment history; return "
        "a structured unavailable payload when neither real source is configured."
    )

    def __init__(self, deploy_history_adapter: DeployHistoryAdapter | None = None):
        super().__init__()
        self._deploy_history_adapter = deploy_history_adapter or DeployHistoryAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        limit = clamp_int(input_args.get("limit"), default=5, minimum=1, maximum=50)
        input_args["limit"] = limit
        if self._deploy_history_adapter.configured:
            try:
                return await self._deploy_history_adapter.query_deployments(service_name, limit)
            except Exception as exc:
                failure = adapter_failure(
                    "deploy_history",
                    exc,
                    summary_prefix="Deploy history query failed",
                    service_name=service_name,
                )
                failure.update({"deployments": [], "recent_change": {}})
                return failure

        payload = adapter_not_configured(
            "deploy_history",
            required_config="DEPLOY_HISTORY_API_URL or MYSQL_DSN",
            summary_prefix="Deploy history query unavailable",
            service_name=service_name,
        )
        payload.update({"deployments": [], "recent_change": {}})
        return payload
