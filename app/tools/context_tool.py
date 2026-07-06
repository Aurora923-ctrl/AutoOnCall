"""Service-context tools backed by CMDB and deployment-history adapters."""

from __future__ import annotations

from typing import Any

from app.config import config
from app.integrations.base import adapter_failure, adapter_not_configured
from app.integrations.service_catalog import CMDBAdapter, DeployHistoryAdapter
from app.services.service_topology import get_service_dependencies
from app.tools.base import AIOpsTool, clamp_int
from app.tools.fallback import run_adapter_or_mock


class QueryServiceContextTool(AIOpsTool):
    name = "query_service_context"
    description = "Query service owner, namespace, dependencies, and business context."
    risk_level = "low"
    read_only = True
    timeout_seconds = 8.0
    data_sources = ["CMDB", "local service topology", "mock"]
    degradation_strategy = (
        "Use CMDB when configured; fall back to local topology, then mock data when enabled "
        "or a structured unavailable payload when mock fallback is disabled."
    )

    def __init__(self, cmdb_adapter: CMDBAdapter | None = None):
        super().__init__()
        self._cmdb_adapter = cmdb_adapter or CMDBAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        cmdb_error: dict[str, Any] | None = None
        if self._cmdb_adapter.configured:
            try:
                return await self._cmdb_adapter.query_service(service_name)
            except Exception as exc:
                cmdb_error = adapter_failure(
                    "cmdb",
                    exc,
                    summary_prefix="CMDB 鏌ヨ澶辫触",
                    service_name=service_name,
                )

        topology_payload = _topology_context_payload(service_name)
        if topology_payload:
            if cmdb_error:
                topology_payload["partial_errors"] = [_adapter_partial_error(cmdb_error)]
                topology_payload["summary"] = (
                    f"{topology_payload['summary']}; CMDB query failed, using local topology"
                )
            return topology_payload

        if cmdb_error and not config.aiops_mock_fallback_enabled:
            cmdb_error.update({"service": {}, "dependencies": []})
            return cmdb_error

        if not config.aiops_mock_fallback_enabled:
            payload = adapter_not_configured(
                "cmdb",
                required_config="CMDB_API_URL",
                summary_prefix="CMDB query unavailable",
                service_name=service_name,
            )
            payload.update({"service": {}, "dependencies": []})
            return payload

        payload = {
            "status": "success",
            "source": "mock",
            "service_name": service_name,
            "service": {
                "service_name": service_name,
                "owner": "demo-oncall",
                "environment": "prod",
                "namespace": service_name.replace("-service", ""),
            },
            "dependencies": ["redis-cluster-prod", "order-mysql"],
            "signals": {"dependency_count": 2, "has_owner": True},
            "summary": f"mock CMDB returned owner and dependencies for {service_name}",
        }
        if cmdb_error:
            payload["partial_errors"] = [_adapter_partial_error(cmdb_error)]
            payload["summary"] = f"{payload['summary']}; CMDB query failed, using mock fallback"
        return payload


class QueryDeployHistoryTool(AIOpsTool):
    name = "query_deploy_history"
    description = "Query recent service deployments, changes, and rollback records."
    risk_level = "low"
    read_only = True
    timeout_seconds = 8.0
    data_sources = ["deployment history API", "mock"]
    degradation_strategy = (
        "Use deployment history API when configured; otherwise return mock deployments when "
        "enabled or a structured unavailable payload when mock fallback is disabled."
    )

    def __init__(self, deploy_history_adapter: DeployHistoryAdapter | None = None):
        super().__init__()
        self._deploy_history_adapter = deploy_history_adapter or DeployHistoryAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        limit = clamp_int(input_args.get("limit"), default=5, minimum=1, maximum=50)
        input_args["limit"] = limit
        return await run_adapter_or_mock(
            configured=self._deploy_history_adapter.configured,
            adapter_call=lambda: self._deploy_history_adapter.query_deployments(
                service_name, limit
            ),
            mock_call=lambda: _mock_deploy_history(service_name),
            source="deploy_history",
            required_config="DEPLOY_HISTORY_API_URL",
            failure_summary_prefix="Deploy history query failed",
            not_configured_summary_prefix="Deploy history query unavailable",
            payload={"service_name": service_name},
            unavailable_defaults={"deployments": [], "recent_change": {}},
        )


def _mock_deploy_history(service_name: str) -> dict[str, Any]:
    deployments = [
        {
            "service_name": service_name,
            "version": "2026.06.27-demo",
            "deployed_at": "2026-06-27T06:30:00Z",
            "operator": "release-bot",
            "status": "succeeded",
        }
    ]
    return {
        "status": "success",
        "source": "mock",
        "service_name": service_name,
        "deployments": deployments,
        "recent_change": deployments[0],
        "signals": {"deployment_count": 1, "latest_status": "succeeded"},
        "summary": f"mock deploy history returned 1 recent change for {service_name}",
    }


def _flatten_topology_dependencies(topology: dict[str, Any]) -> list[str]:
    dependencies: list[str] = []
    for value in topology.values():
        if isinstance(value, list):
            dependencies.extend(str(item) for item in value if item)
    return dependencies


def _topology_context_payload(service_name: str) -> dict[str, Any] | None:
    topology = get_service_dependencies(service_name)
    if not topology:
        return None
    dependencies = _flatten_topology_dependencies(topology)
    return {
        "status": "success",
        "source": "rule_based",
        "service_name": service_name,
        "service": {"service_name": service_name, "dependencies": dependencies},
        "dependencies": dependencies,
        "signals": {"dependency_count": len(dependencies), "topology_configured": True},
        "summary": f"local topology returned {len(dependencies)} dependencies for {service_name}",
    }


def _adapter_partial_error(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": payload.get("source") or "unknown",
        "error_type": payload.get("error_type") or "adapter_error",
        "error_message": payload.get("error_message") or payload.get("message") or "unknown error",
    }
