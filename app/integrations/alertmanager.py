"""Alertmanager HTTP API adapter."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import config
from app.integrations.base import adapter_success, bearer_headers, require_config


class AlertmanagerAlertAdapter:
    """Read current alerts from Alertmanager for incident context."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = (base_url if base_url is not None else config.alertmanager_base_url).rstrip(
            "/"
        )
        self.token = token if token is not None else config.alertmanager_bearer_token
        self.timeout_seconds = timeout_seconds or config.alertmanager_timeout_seconds
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def query_alerts(
        self,
        service_name: str,
        state: str = "active",
        limit: int = 20,
    ) -> dict[str, Any]:
        base_url = require_config(self.base_url, "ALERTMANAGER_BASE_URL")
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=bearer_headers(self.token),
            transport=self.transport,
        ) as client:
            response = await client.get(
                f"{base_url}/api/v2/alerts",
                params={"active": str(state != "resolved").lower(), "silenced": "false"},
            )
            response.raise_for_status()
            payload = response.json()

        alerts = payload if isinstance(payload, list) else []
        normalized = [
            alert
            for alert in (self._alert_summary(item) for item in alerts)
            if self._matches_service(alert, service_name)
        ][: max(limit, 0)]
        severities = {
            str(alert.get("severity") or "unknown") for alert in normalized if alert.get("severity")
        }
        return adapter_success(
            source="alertmanager",
            summary=f"Alertmanager 返回 {len(normalized)} 条 {service_name} 相关告警",
            signals={
                "alert_count": len(normalized),
                "firing_count": sum(1 for alert in normalized if alert.get("state") == "active"),
                "severity_count": len(severities),
            },
            raw={"alerts": alerts[: min(len(alerts), limit)]},
            service_name=service_name,
            alerts=normalized,
        )

    @staticmethod
    def _alert_summary(alert: dict[str, Any]) -> dict[str, Any]:
        raw_labels = alert.get("labels")
        raw_annotations = alert.get("annotations")
        raw_status = alert.get("status")
        labels: dict[str, Any] = raw_labels if isinstance(raw_labels, dict) else {}
        annotations: dict[str, Any] = raw_annotations if isinstance(raw_annotations, dict) else {}
        status: dict[str, Any] = raw_status if isinstance(raw_status, dict) else {}
        return {
            "alertname": labels.get("alertname", ""),
            "service_name": labels.get("service") or labels.get("service_name") or "",
            "severity": labels.get("severity", ""),
            "state": status.get("state", ""),
            "starts_at": alert.get("startsAt", ""),
            "ends_at": alert.get("endsAt", ""),
            "summary": annotations.get("summary", ""),
            "description": annotations.get("description", ""),
            "labels": labels,
        }

    @staticmethod
    def _matches_service(alert: dict[str, Any], service_name: str) -> bool:
        if service_name == "unknown-service":
            return True
        text = " ".join(
            str(alert.get(key) or "")
            for key in ["service_name", "alertname", "summary", "description"]
        )
        return service_name.lower() in text.lower()
