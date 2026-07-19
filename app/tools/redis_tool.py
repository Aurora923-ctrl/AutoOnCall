"""Redis status tool backed by Redis INFO and live incident evidence keys."""

from __future__ import annotations

from typing import Any

from app.integrations.base import adapter_failure, adapter_not_configured
from app.integrations.redis_info import RedisInfoAdapter
from app.services.service_topology import get_primary_dependency_instance
from app.tools.base import AIOpsTool, ToolRetryPolicy, clamp_duration


class QueryRedisStatusTool(AIOpsTool):
    """Query Redis connection, maxclients, memory, slowlog, and demo incident signals."""

    name = "query_redis_status"
    description = "Query Redis connection, maxclients, memory, and slowlog status."
    input_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string", "minLength": 1},
            "time_range": {"type": "string", "default": "10m"},
            "redis_instance": {"type": "string"},
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
    data_sources = ["Redis INFO", "Redis incident evidence keys", "service topology"]
    degradation_strategy = (
        "Use Redis INFO and configured Redis evidence keys when available; otherwise return "
        "a structured unavailable payload."
    )

    def __init__(self, redis_adapter: RedisInfoAdapter | None = None):
        super().__init__()
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
            maximum_seconds=30 * 86400,
        )
        input_args["time_range"] = time_range

        payload = {
            "service_name": service_name,
            "redis_instance": redis_instance,
            "time_range": time_range,
        }
        if not self._redis_adapter.configured:
            return adapter_not_configured(
                "redis_info",
                required_config="REDIS_URL, REDIS_INSTANCES, or REDIS_HOST",
                summary_prefix="Redis status query unavailable",
                **payload,
            )
        try:
            return await self._redis_adapter.query_status(service_name, redis_instance, time_range)
        except Exception as exc:
            return adapter_failure(
                "redis_info",
                exc,
                summary_prefix="Redis INFO query failed",
                **payload,
            )
