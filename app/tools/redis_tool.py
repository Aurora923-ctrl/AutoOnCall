"""Redis status tool with deterministic mock output."""

from __future__ import annotations

from typing import Any

from app.config import config
from app.integrations.base import adapter_failure, adapter_not_configured
from app.integrations.redis_info import RedisInfoAdapter
from app.services.service_topology import get_primary_dependency_instance
from app.tools.base import AIOpsTool, clamp_duration


class QueryRedisStatusTool(AIOpsTool):
    """Query or mock Redis connection, maxclients, memory, and slowlog status."""

    name = "query_redis_status"
    description = "查询 Redis 连接数、maxclients、内存使用和慢日志状态"
    input_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string"},
            "time_range": {"type": "string", "default": "10m"},
            "redis_instance": {"type": "string"},
        },
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    timeout_seconds = 8.0
    data_sources = ["Redis INFO", "service topology", "mock"]
    degradation_strategy = (
        "Redis 实例不可达时使用拓扑推断实例并返回 mock 连接数证据；关闭 mock 后返回结构化不可用结果"
    )

    def __init__(self, redis_adapter: RedisInfoAdapter | None = None):
        super().__init__()
        self._allow_adapter_failure_fallback = redis_adapter is None
        self._redis_adapter = redis_adapter or RedisInfoAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        redis_instance = (
            input_args.get("redis_instance")
            or get_primary_dependency_instance(service_name, "redis")
            or "redis-cluster-prod"
        )
        time_range = clamp_duration(
            input_args.get("time_range"),
            default="10m",
            maximum_seconds=3600,
        )
        input_args["time_range"] = time_range

        if self._redis_adapter.configured:
            try:
                return await self._redis_adapter.query_status(
                    service_name, redis_instance, time_range
                )
            except Exception as exc:
                if config.aiops_mock_fallback_enabled and self._allow_adapter_failure_fallback:
                    return self._mock_status(service_name, redis_instance, time_range)
                payload = adapter_failure(
                    "redis_info",
                    exc,
                    summary_prefix="Redis INFO 查询失败",
                    service_name=service_name,
                    redis_instance=redis_instance,
                    time_range=time_range,
                )
                return payload

        if not config.aiops_mock_fallback_enabled:
            return adapter_not_configured(
                "redis_info",
                required_config="REDIS_HOST",
                summary_prefix="Redis 状态查询不可用",
                service_name=service_name,
                redis_instance=redis_instance,
                time_range=time_range,
            )

        return self._mock_status(service_name, redis_instance, time_range)

    @staticmethod
    def _mock_status(service_name: str, redis_instance: str, time_range: str) -> dict[str, Any]:
        connected_clients = 9940
        maxclients = 10000
        usage = connected_clients / maxclients
        exhausted = usage >= 0.9

        return {
            "service_name": service_name,
            "redis_instance": redis_instance,
            "time_range": time_range,
            "source": "mock",
            "connected_clients": connected_clients,
            "maxclients": maxclients,
            "client_usage_ratio": round(usage, 4),
            "blocked_clients": 37,
            "used_memory_human": "12.4G",
            "maxmemory_human": "16G",
            "slowlog": [
                {"command": "HGETALL order:cache:*", "duration_ms": 128, "count": 26},
                {"command": "ZRANGE order:queue 0 -1", "duration_ms": 94, "count": 14},
            ],
            "alert_info": {
                "triggered": exhausted,
                "message": (
                    "Redis connected_clients 接近 maxclients，疑似连接数耗尽"
                    if exhausted
                    else "Redis 连接数处于正常范围"
                ),
            },
            "summary": (
                f"{redis_instance} connected_clients={connected_clients}/{maxclients}, "
                f"usage={usage:.2%}"
            ),
        }
