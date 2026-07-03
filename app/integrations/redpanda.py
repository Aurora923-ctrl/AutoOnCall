"""Redpanda/Kafka read-only status adapter."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import config
from app.integrations.base import adapter_success, bearer_headers, require_config


class RedpandaStatusAdapter:
    """Read Redpanda cluster readiness and optional partition metadata."""

    def __init__(
        self,
        admin_url: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.admin_url = (admin_url if admin_url is not None else config.redpanda_admin_url).rstrip(
            "/"
        )
        self.token = config.redpanda_bearer_token
        self.timeout_seconds = timeout_seconds or config.redpanda_timeout_seconds
        self.bootstrap_servers = config.kafka_bootstrap_servers
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.admin_url)

    async def query_status(
        self,
        service_name: str,
        topic: str = "",
    ) -> dict[str, Any]:
        base_url = require_config(self.admin_url, "REDPANDA_ADMIN_URL")
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=bearer_headers(self.token),
            transport=self.transport,
        ) as client:
            ready_payload = await self._get_json_or_text(client, f"{base_url}/v1/status/ready")
            partitions_payload = await self._best_effort_json(client, f"{base_url}/v1/partitions")

        partitions = partitions_payload if isinstance(partitions_payload, list) else []
        topic_partitions = [
            item
            for item in partitions
            if isinstance(item, dict) and (not topic or item.get("topic") == topic)
        ]
        topics = sorted(
            {
                str(item.get("topic"))
                for item in partitions
                if isinstance(item, dict) and item.get("topic")
            }
        )
        return adapter_success(
            source="redpanda",
            summary=(
                f"Redpanda ready，topics={len(topics)}, matched_partitions={len(topic_partitions)}"
            ),
            signals={
                "ready": True,
                "topic_count": len(topics),
                "partition_count": len(partitions),
                "matched_partition_count": len(topic_partitions),
            },
            raw={"ready": ready_payload, "partitions_sample": partitions[:10]},
            service_name=service_name,
            topic=topic,
            bootstrap_servers=self.bootstrap_servers,
            topics=topics[:50],
            partitions=topic_partitions[:20],
        )

    async def _get_json_or_text(self, client: httpx.AsyncClient, url: str) -> Any:
        response = await client.get(url)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return {"body": response.text}

    async def _best_effort_json(self, client: httpx.AsyncClient, url: str) -> Any:
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
        except Exception:
            return []
