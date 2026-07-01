"""Operational tools with optional real adapters and deterministic mock fallback."""

from __future__ import annotations

from typing import Any

from app.config import config
from app.integrations.base import adapter_failure, adapter_not_configured
from app.integrations.kubernetes import KubernetesStatusAdapter
from app.integrations.mysql import MySQLStatusAdapter
from app.integrations.ticketing import TicketingAdapter
from app.services.service_topology import get_primary_dependency_instance
from app.tools.base import AIOpsTool, clamp_duration, clamp_int


class QueryK8sStatusTool(AIOpsTool):
    name = "query_k8s_status"
    description = "查询 Pod 状态、重启次数、镜像版本和部署时间"
    risk_level = "low"
    read_only = True
    data_sources = ["Kubernetes API", "mock"]
    degradation_strategy = (
        "Kubernetes API 不可用时返回 mock Pod 状态；关闭 mock 后返回结构化不可用结果"
    )

    def __init__(self, k8s_adapter: KubernetesStatusAdapter | None = None):
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
        if self._k8s_adapter.configured:
            try:
                return await self._k8s_adapter.query_service_status(service_name, time_range)
            except Exception as exc:
                if config.aiops_mock_fallback_enabled and self._allow_adapter_failure_fallback:
                    return self._mock_status(service_name, time_range)
                payload = adapter_failure(
                    "kubernetes",
                    exc,
                    summary_prefix="Kubernetes 查询失败",
                    service_name=service_name,
                    time_range=time_range,
                )
                payload.update(
                    {
                        "pods": [],
                        "events": [],
                    }
                )
                return payload
        if not config.aiops_mock_fallback_enabled:
            payload = adapter_not_configured(
                "kubernetes",
                required_config="KUBERNETES_API_SERVER",
                summary_prefix="Kubernetes 状态查询不可用",
                service_name=service_name,
                time_range=time_range,
            )
            payload.update({"pods": [], "events": []})
            return payload
        return self._mock_status(service_name, time_range)

    @staticmethod
    def _mock_status(service_name: str, time_range: str) -> dict[str, Any]:
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
                    "image": f"{service_name}:2026.06.30",
                    "updated_at": "2026-06-30 10:15:00",
                },
                "summary": "Pod 出现 CrashLoopBackOff，主实例重启 12 次且最近一次为 OOMKilled",
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
                "image": f"{service_name}:2026.06.22",
                "updated_at": "2026-06-22 15:40:00",
            },
            "summary": "Pod 整体运行，未发现大规模 CrashLoopBackOff",
        }


class QueryMySQLStatusTool(AIOpsTool):
    name = "query_mysql_status"
    description = "查询 MySQL 慢 SQL、连接池、锁等待和活跃连接数"
    risk_level = "low"
    read_only = True
    data_sources = ["MySQL status SQL", "service topology", "mock"]
    degradation_strategy = (
        "MySQL 不可达时使用拓扑推断实例并返回 mock 慢查询证据；关闭 mock 后返回结构化不可用结果"
    )

    def __init__(self, mysql_adapter: MySQLStatusAdapter | None = None):
        self._allow_adapter_failure_fallback = mysql_adapter is None
        self._mysql_adapter = mysql_adapter or MySQLStatusAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        mysql_instance = (
            input_args.get("mysql_instance")
            or get_primary_dependency_instance(service_name, "mysql")
            or ""
        )
        if self._mysql_adapter.configured:
            try:
                return await self._mysql_adapter.query_status(service_name, mysql_instance)
            except Exception as exc:
                if config.aiops_mock_fallback_enabled and self._allow_adapter_failure_fallback:
                    return self._mock_status(service_name, mysql_instance)
                payload = adapter_failure(
                    "mysql",
                    exc,
                    summary_prefix="MySQL 查询失败",
                    service_name=service_name,
                    mysql_instance=mysql_instance,
                )
                payload.update(
                    {
                        "slow_queries": [],
                        "connections": {},
                        "lock_waits": 0,
                    }
                )
                return payload
        if not config.aiops_mock_fallback_enabled:
            payload = adapter_not_configured(
                "mysql",
                required_config="MYSQL_DSN",
                summary_prefix="MySQL 状态查询不可用",
                service_name=service_name,
                mysql_instance=mysql_instance,
            )
            payload.update({"slow_queries": [], "connections": {}, "lock_waits": 0})
            return payload
        return self._mock_status(service_name, mysql_instance)

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
            "summary": "MySQL 有少量慢查询，但连接池未耗尽",
        }


class SearchHistoryTicketTool(AIOpsTool):
    name = "search_history_ticket"
    description = "检索历史相似故障工单"
    risk_level = "low"
    read_only = True
    data_sources = ["ticketing API", "mock"]
    degradation_strategy = "工单系统不可用时返回演示相似故障；关闭 mock 后返回结构化不可用结果"

    def __init__(self, ticketing_adapter: TicketingAdapter | None = None):
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
                if config.aiops_mock_fallback_enabled and self._allow_adapter_failure_fallback:
                    return self._mock_history(service_name)
                payload = adapter_failure(
                    "ticket_api",
                    exc,
                    summary_prefix="工单系统查询失败",
                    service_name=service_name,
                )
                payload.update(
                    {
                        "tickets": [],
                    }
                )
                return payload
        if not config.aiops_mock_fallback_enabled:
            payload = adapter_not_configured(
                "ticket_api",
                required_config="TICKET_API_URL",
                summary_prefix="历史工单查询不可用",
                service_name=service_name,
                query=query,
                limit=limit,
            )
            payload.update({"tickets": []})
            return payload
        return self._mock_history(service_name)

    @staticmethod
    def _mock_history(service_name: str) -> dict[str, Any]:
        return {
            "service_name": service_name,
            "source": "mock",
            "tickets": [
                {
                    "ticket_id": "INC-2026-0618-REDIS-001",
                    "title": "order-service Redis maxclients exhausted",
                    "root_cause": "Redis 连接数达到 maxclients，应用连接池未及时释放连接",
                    "resolution": "释放异常连接，调整连接池上限，增加 maxclients 监控",
                }
            ],
            "summary": "找到 1 条 Redis 连接数耗尽相似故障",
        }


class SuggestRemediationTool(AIOpsTool):
    name = "suggest_remediation"
    description = "根据证据生成修复建议，但不直接执行危险操作"
    risk_level = "medium"
    read_only = True
    data_sources = ["diagnosis evidence", "rule based policy"]
    degradation_strategy = "仅生成建议不执行动作；涉及变更的建议交由审批和安全变更流程处理"

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        return {
            "service_name": service_name,
            "source": "rule_based",
            "short_term": [
                "释放异常 Redis 连接或重启异常客户端实例前先提交审批",
                "临时限流高流量接口，降低 Redis 连接压力",
                "检查连接池泄漏并降低空闲连接保留时间",
            ],
            "medium_term": [
                "优化 Redis 连接池配置和超时策略",
                "增加 connected_clients/maxclients 告警",
            ],
            "risk_level": "medium",
            "change_requires_approval": True,
            "approval_scope": "suggested_change_only",
            "summary": "修复建议已生成；建议中的真实变更动作需人工审批",
        }
