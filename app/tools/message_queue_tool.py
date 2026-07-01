"""Message queue status tool backed by Redpanda/Kafka adapters."""

from __future__ import annotations

from typing import Any

from app.config import config
from app.integrations.base import adapter_failure, adapter_not_configured
from app.integrations.redpanda import RedpandaStatusAdapter
from app.tools.base import AIOpsTool


class QueryMessageQueueStatusTool(AIOpsTool):
    name = "query_message_queue_status"
    description = (
        "通过 Redpanda Admin API 查询 Kafka-compatible topic、partition 和集群 readiness 状态"
    )
    risk_level = "low"
    read_only = True
    timeout_seconds = 10.0
    data_sources = ["Redpanda Admin API", "mock"]
    degradation_strategy = (
        "消息队列管理端不可用时返回 mock topic readiness；关闭 mock 后返回结构化不可用结果"
    )

    def __init__(self, redpanda_adapter: RedpandaStatusAdapter | None = None):
        self._redpanda_adapter = redpanda_adapter or RedpandaStatusAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        topic = input_args.get("topic") or ""
        if self._redpanda_adapter.configured:
            try:
                return await self._redpanda_adapter.query_status(service_name, topic)
            except Exception as exc:
                payload = adapter_failure(
                    "redpanda",
                    exc,
                    summary_prefix="Redpanda/Kafka 查询失败",
                    service_name=service_name,
                    topic=topic,
                )
                payload.update({"topics": [], "partitions": []})
                return payload
        if not config.aiops_mock_fallback_enabled:
            payload = adapter_not_configured(
                "redpanda",
                required_config="REDPANDA_ADMIN_URL",
                summary_prefix="消息队列查询不可用",
                service_name=service_name,
                topic=topic,
            )
            payload.update({"topics": [], "partitions": []})
            return payload
        return self._mock_status(service_name, topic)

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
                    f"mock Redpanda 返回 {topic_name} consumer lag 高，"
                    "总积压 128400，最大分区积压 79000"
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
            "summary": "mock Redpanda 返回 topic 正常，无 consumer lag 积压",
        }
