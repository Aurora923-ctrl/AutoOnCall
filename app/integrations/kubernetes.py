"""Kubernetes API adapter for read-only pod and deployment status."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import config
from app.integrations.base import (
    ExternalAdapterNotFoundError,
    ExternalAdapterResponseError,
    adapter_success,
    bearer_headers,
    classify_adapter_error,
    parse_duration_seconds,
    public_adapter_failure_message,
    require_config,
    require_kubernetes_label_value,
    require_success_payload,
)


class KubernetesStatusAdapter:
    """Read Kubernetes workload status through the Kubernetes HTTP API."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.api_server = config.kubernetes_api_server.rstrip("/")
        self.namespace = config.kubernetes_namespace
        self.token = config.kubernetes_bearer_token
        self.verify_ssl = config.kubernetes_verify_ssl
        self.timeout_seconds = config.kubernetes_timeout_seconds
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.api_server)

    async def query_service_status(
        self, service_name: str, time_range: str = "10m"
    ) -> dict[str, Any]:
        api_server = require_config(self.api_server, "KUBERNETES_API_SERVER")
        label_value = require_kubernetes_label_value(service_name, field_name="service_name")
        selector = f"app={label_value}"
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=bearer_headers(self.token),
            verify=self.verify_ssl,
            transport=self.transport,
        ) as client:
            pod_response = await client.get(
                f"{api_server}/api/v1/namespaces/{self.namespace}/pods",
                params={"labelSelector": selector},
            )
            pod_response.raise_for_status()
            pods_payload = require_success_payload(
                pod_response.json(),
                system_name="Kubernetes pods API",
            )
            if not isinstance(pods_payload.get("items"), list):
                raise ExternalAdapterResponseError("Kubernetes pods response missing items")
            events_payload: dict[str, Any] = {"items": []}
            events_error: dict[str, Any] | None = None
            try:
                event_response = await client.get(
                    f"{api_server}/api/v1/namespaces/{self.namespace}/events",
                    params={"fieldSelector": "involvedObject.kind=Pod"},
                )
                event_response.raise_for_status()
                events_payload = require_success_payload(
                    event_response.json(),
                    system_name="Kubernetes events API",
                )
                if not isinstance(events_payload.get("items"), list):
                    raise ExternalAdapterResponseError("Kubernetes events response missing items")
            except Exception as exc:
                error_type = classify_adapter_error(exc)
                events_error = {
                    "query": "events",
                    "error_type": error_type,
                    "error_message": public_adapter_failure_message(error_type),
                }

        pods = [self._pod_summary(item) for item in pods_payload.get("items", [])]
        if not pods:
            raise ExternalAdapterNotFoundError(
                f"No Kubernetes pods matched service {service_name!r}"
            )
        pod_names = {pod["name"] for pod in pods if pod.get("name")}
        window_seconds = parse_duration_seconds(time_range)
        window_started_at = datetime.now(UTC) - timedelta(seconds=window_seconds)
        events = [
            self._event_summary(item)
            for item in events_payload.get("items", [])
            if item.get("involvedObject", {}).get("name") in pod_names
            and self._event_within_window(item, window_started_at)
        ]
        restart_count = sum(int(pod.get("restarts", 0)) for pod in pods)
        not_ready_count = sum(1 for pod in pods if not pod.get("ready"))
        warning_count = sum(1 for event in events if event.get("type") == "Warning")
        return adapter_success(
            source="kubernetes",
            summary=(
                f"Kubernetes 返回 {len(pods)} 个 Pod，"
                f"restart_count={restart_count}, warnings={warning_count}"
            ),
            signals={
                "pod_count": len(pods),
                "not_ready_count": not_ready_count,
                "restart_count": restart_count,
                "warning_event_count": warning_count,
            },
            raw={
                "pods": pods,
                "events": events,
                "raw_truncated": True,
            },
            service_name=service_name,
            namespace=self.namespace,
            time_range=time_range,
            event_window_seconds=window_seconds,
            pods=pods,
            events=events,
            partial_errors=[events_error] if events_error else [],
        )

    @staticmethod
    def _pod_summary(item: dict[str, Any]) -> dict[str, Any]:
        status = item.get("status", {})
        containers = status.get("containerStatuses", [])
        restarts = sum(int(container.get("restartCount", 0)) for container in containers)
        ready = bool(containers) and all(bool(container.get("ready")) for container in containers)
        return {
            "name": item.get("metadata", {}).get("name", ""),
            "ready": ready,
            "restarts": restarts,
            "status": status.get("phase", "Unknown"),
            "started_at": status.get("startTime", ""),
        }

    @staticmethod
    def _event_summary(item: dict[str, Any]) -> dict[str, Any]:
        involved = item.get("involvedObject", {})
        return {
            "pod": involved.get("name", ""),
            "type": item.get("type", ""),
            "reason": item.get("reason", ""),
            "message_present": bool(item.get("message")),
            "count": item.get("count", 1),
            "last_timestamp": item.get("lastTimestamp") or item.get("eventTime") or "",
        }

    @classmethod
    def _event_within_window(
        cls,
        item: dict[str, Any],
        window_started_at: datetime,
    ) -> bool:
        event_time = cls._event_observed_at(item)
        if event_time is None:
            return True
        return event_time >= window_started_at

    @staticmethod
    def _event_observed_at(item: dict[str, Any]) -> datetime | None:
        timestamp = (
            item.get("lastTimestamp")
            or item.get("eventTime")
            or item.get("series", {}).get("lastObservedTime")
            or item.get("metadata", {}).get("creationTimestamp")
            or item.get("firstTimestamp")
        )
        if not timestamp:
            return None
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
