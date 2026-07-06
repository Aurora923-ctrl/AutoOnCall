"""Redis status tool with deterministic mock output."""

from __future__ import annotations

from typing import Any

from app.integrations.redis_info import RedisInfoAdapter
from app.services.service_topology import get_primary_dependency_instance
from app.tools.base import AIOpsTool, clamp_duration
from app.tools.fallback import run_adapter_or_mock


class QueryRedisStatusTool(AIOpsTool):
    """Query or mock Redis connection, maxclients, memory, and slowlog status."""

    name = "query_redis_status"
    description = "Query Redis connection, maxclients, memory, and slowlog status."
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
        "Use Redis INFO when configured; otherwise return mock data when enabled or a "
        "structured unavailable payload when mock fallback is disabled."
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

        return await run_adapter_or_mock(
            configured=self._redis_adapter.configured,
            adapter_call=lambda: self._redis_adapter.query_status(
                service_name, redis_instance, time_range
            ),
            mock_call=lambda: self._mock_status(service_name, redis_instance, time_range),
            source="redis_info",
            required_config="REDIS_HOST",
            failure_summary_prefix="Redis INFO query failed",
            not_configured_summary_prefix="Redis status query unavailable",
            payload={
                "service_name": service_name,
                "redis_instance": redis_instance,
                "time_range": time_range,
            },
            allow_failure_fallback=self._allow_adapter_failure_fallback,
        )

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
                    "Redis connected_clients is close to maxclients"
                    if exhausted
                    else "Redis connection count is within normal range"
                ),
            },
            "summary": (
                f"{redis_instance} connected_clients={connected_clients}/{maxclients}, "
                f"usage={usage:.2%}"
            ),
        }
