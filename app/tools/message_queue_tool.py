"""Message queue status tool backed by Redpanda/Kafka adapters."""

from __future__ import annotations

from typing import Any

from app.integrations.redpanda import RedpandaStatusAdapter
from app.tools.base import AIOpsTool
from app.tools.fallback import run_adapter_or_mock


class QueryMessageQueueStatusTool(AIOpsTool):
    name = "query_message_queue_status"
    description = "Query Redpanda/Kafka topic, partition, and readiness status."
    risk_level = "low"
    read_only = True
    timeout_seconds = 10.0
    data_sources = ["Redpanda Admin API", "mock"]
    degradation_strategy = (
        "Use Redpanda/Kafka adapters when configured; otherwise return mock queue data "
        "when enabled or a structured unavailable payload when mock fallback is disabled."
    )

    def __init__(self, redpanda_adapter: RedpandaStatusAdapter | None = None):
        super().__init__()
        self._redpanda_adapter = redpanda_adapter or RedpandaStatusAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        topic = input_args.get("topic") or ""
        return await run_adapter_or_mock(
            configured=self._redpanda_adapter.configured,
            adapter_call=lambda: self._redpanda_adapter.query_status(service_name, topic),
            mock_call=lambda: self._mock_status(service_name, topic),
            source="redpanda",
            required_config="REDPANDA_ADMIN_URL",
            failure_summary_prefix="Redpanda/Kafka query failed",
            not_configured_summary_prefix="Message queue query unavailable",
            payload={"service_name": service_name, "topic": topic},
            unavailable_defaults={"topics": [], "partitions": []},
        )

    @staticmethod
    def _mock_status(service_name: str, topic: str) -> dict[str, Any]:
        topic_name = topic or "redpanda-orders"
        if service_name == "checkout-service" or "checkout" in topic_name:
            return {
                "status": "success",
                "source": "mock",
                "service_name": service_name,
                "topic": topic_name,
                "topics": [topic_name],
                "partitions": [
                    {
                        "topic": topic_name,
                        "partition": 0,
                        "leader": 1,
                        "consumer_lag": 79000,
                        "status": "lagging",
                    },
                    {
                        "topic": topic_name,
                        "partition": 1,
                        "leader": 2,
                        "consumer_lag": 32600,
                        "status": "lagging",
                    },
                    {
                        "topic": topic_name,
                        "partition": 2,
                        "leader": 3,
                        "consumer_lag": 16800,
                        "status": "lagging",
                    },
                ],
                "signals": {
                    "ready": True,
                    "topic_count": 1,
                    "partition_count": 3,
                    "consumer_lag": 128400,
                    "max_partition_lag": 79000,
                    "under_replicated_partitions": 0,
                },
                "summary": (
                    f"mock Redpanda returned high consumer lag for {topic_name}; "
                    "total_lag=128400, max_partition_lag=79000"
                ),
            }
        return {
            "status": "success",
            "source": "mock",
            "service_name": service_name,
            "topic": topic_name,
            "topics": [topic_name],
            "partitions": [
                {
                    "topic": topic_name,
                    "partition": 0,
                    "leader": 1,
                    "consumer_lag": 0,
                    "status": "ready",
                }
            ],
            "signals": {
                "ready": True,
                "topic_count": 1,
                "partition_count": 3,
                "consumer_lag": 0,
                "max_partition_lag": 0,
                "under_replicated_partitions": 0,
            },
            "summary": "mock Redpanda returned a healthy topic with no consumer lag",
        }
