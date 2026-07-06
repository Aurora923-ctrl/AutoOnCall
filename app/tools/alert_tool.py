"""Alert query tool backed by Alertmanager with mock fallback."""

from __future__ import annotations

from typing import Any

from app.integrations.alertmanager import AlertmanagerAlertAdapter
from app.tools.base import AIOpsTool, clamp_int
from app.tools.fallback import run_adapter_or_mock


class QueryAlertsTool(AIOpsTool):
    name = "query_alerts"
    description = "Query current Alertmanager alerts for incident context."
    input_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string"},
            "state": {"type": "string", "default": "active"},
            "limit": {"type": "integer", "default": 20},
        },
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    timeout_seconds = 8.0
    data_sources = ["Alertmanager", "mock"]
    degradation_strategy = (
        "Use Alertmanager when configured; otherwise return mock alerts when enabled or a "
        "structured unavailable payload when mock fallback is disabled."
    )

    def __init__(self, alert_adapter: AlertmanagerAlertAdapter | None = None):
        super().__init__()
        self._allow_adapter_failure_fallback = alert_adapter is None
        self._alert_adapter = alert_adapter or AlertmanagerAlertAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        state = input_args.get("state") or "active"
        limit = clamp_int(input_args.get("limit"), default=20, minimum=1, maximum=100)
        input_args["limit"] = limit
        return await run_adapter_or_mock(
            configured=self._alert_adapter.configured,
            adapter_call=lambda: self._alert_adapter.query_alerts(service_name, state, limit),
            mock_call=lambda: self._mock_alerts(service_name),
            source="alertmanager",
            required_config="ALERTMANAGER_BASE_URL",
            failure_summary_prefix="Alertmanager query failed",
            not_configured_summary_prefix="Alert query unavailable",
            payload={"service_name": service_name, "state": state},
            unavailable_defaults={"alerts": []},
            allow_failure_fallback=self._allow_adapter_failure_fallback,
        )

    @staticmethod
    def _mock_alerts(service_name: str) -> dict[str, Any]:
        alerts = [
            {
                "alertname": "HighErrorRate",
                "service_name": service_name,
                "severity": "critical",
                "state": "active",
                "summary": f"{service_name} 5xx error rate exceeded threshold",
            }
        ]
        return {
            "status": "success",
            "source": "mock",
            "service_name": service_name,
            "alerts": alerts,
            "signals": {"alert_count": len(alerts), "firing_count": len(alerts)},
            "summary": f"mock Alertmanager returned {len(alerts)} active alerts",
        }
