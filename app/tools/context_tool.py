"""Service-context tools backed by CMDB and deployment-history adapters."""

from __future__ import annotations

from typing import Any

from app.config import config
from app.integrations.base import adapter_failure, adapter_not_configured
from app.integrations.service_catalog import CMDBAdapter, DeployHistoryAdapter
from app.services.service_topology import get_service_dependencies
from app.tools.base import AIOpsTool


class QueryServiceContextTool(AIOpsTool):
    name = "query_service_context"
    description = "查询服务 owner、namespace、上下游依赖和业务上下文"
    risk_level = "low"
    read_only = True
    timeout_seconds = 8.0
    data_sources = ["CMDB", "local service topology", "mock"]
    degradation_strategy = (
        "CMDB 不可用时优先读取本地拓扑配置；仍无结果时按 mock 策略返回 owner 和依赖"
    )

    def __init__(self, cmdb_adapter: CMDBAdapter | None = None):
        self._cmdb_adapter = cmdb_adapter or CMDBAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        if self._cmdb_adapter.configured:
            try:
                return await self._cmdb_adapter.query_service(service_name)
            except Exception as exc:
                payload = adapter_failure(
                    "cmdb",
                    exc,
                    summary_prefix="CMDB 查询失败",
                    service_name=service_name,
                )
                payload.update({"service": {}, "dependencies": []})
                return payload

        topology = get_service_dependencies(service_name)
        if topology:
            dependencies = _flatten_topology_dependencies(topology)
            return {
                "status": "success",
                "source": "rule_based",
                "service_name": service_name,
                "service": {"service_name": service_name, "dependencies": dependencies},
                "dependencies": dependencies,
                "signals": {"dependency_count": len(dependencies), "topology_configured": True},
                "summary": f"本地拓扑返回 {service_name} 的 {len(dependencies)} 个依赖",
            }

        if not config.aiops_mock_fallback_enabled:
            payload = adapter_not_configured(
                "cmdb",
                required_config="CMDB_API_URL",
                summary_prefix="CMDB 查询不可用",
                service_name=service_name,
            )
            payload.update({"service": {}, "dependencies": []})
            return payload

        return {
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
            "summary": f"mock CMDB 返回 {service_name} 的 owner 和依赖关系",
        }


class QueryDeployHistoryTool(AIOpsTool):
    name = "query_deploy_history"
    description = "查询服务近期发布、变更和回滚记录，用于变更关联分析"
    risk_level = "low"
    read_only = True
    timeout_seconds = 8.0
    data_sources = ["deployment history API", "mock"]
    degradation_strategy = "发布系统不可用时返回演示发布记录；关闭 mock 后返回结构化不可用结果"

    def __init__(self, deploy_history_adapter: DeployHistoryAdapter | None = None):
        self._deploy_history_adapter = deploy_history_adapter or DeployHistoryAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        limit = int(input_args.get("limit") or 5)
        if self._deploy_history_adapter.configured:
            try:
                return await self._deploy_history_adapter.query_deployments(service_name, limit)
            except Exception as exc:
                payload = adapter_failure(
                    "deploy_history",
                    exc,
                    summary_prefix="发布历史查询失败",
                    service_name=service_name,
                )
                payload.update({"deployments": [], "recent_change": {}})
                return payload

        if not config.aiops_mock_fallback_enabled:
            payload = adapter_not_configured(
                "deploy_history",
                required_config="DEPLOY_HISTORY_API_URL",
                summary_prefix="发布历史查询不可用",
                service_name=service_name,
            )
            payload.update({"deployments": [], "recent_change": {}})
            return payload

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
            "summary": f"mock 发布历史返回 {service_name} 最近 1 条变更",
        }


def _flatten_topology_dependencies(topology: dict[str, Any]) -> list[str]:
    dependencies: list[str] = []
    for value in topology.values():
        if isinstance(value, list):
            dependencies.extend(str(item) for item in value if item)
    return dependencies
