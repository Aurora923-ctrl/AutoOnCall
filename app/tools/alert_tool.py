"""Alert query tool backed by Alertmanager with mock fallback."""

from __future__ import annotations

from typing import Any

from app.config import config
from app.integrations.alertmanager import AlertmanagerAlertAdapter
from app.integrations.base import adapter_failure, adapter_not_configured
from app.tools.base import AIOpsTool


class QueryAlertsTool(AIOpsTool):
    name = "query_alerts"
    description = "查询 Alertmanager 当前告警，确认 Incident 输入和告警上下文"
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
        "Alertmanager 未配置或短暂失败时返回演示告警；关闭 mock 后返回结构化不可用结果"
    )

    def __init__(self, alert_adapter: AlertmanagerAlertAdapter | None = None):
        self._allow_adapter_failure_fallback = alert_adapter is None
        self._alert_adapter = alert_adapter or AlertmanagerAlertAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        state = input_args.get("state") or "active"
        limit = int(input_args.get("limit") or 20)
        if self._alert_adapter.configured:
            try:
                return await self._alert_adapter.query_alerts(service_name, state, limit)
            except Exception as exc:
                if config.aiops_mock_fallback_enabled and self._allow_adapter_failure_fallback:
                    return self._mock_alerts(service_name)
                payload = adapter_failure(
                    "alertmanager",
                    exc,
                    summary_prefix="Alertmanager 查询失败",
                    service_name=service_name,
                    state=state,
                )
                payload.update({"alerts": []})
                return payload

        if not config.aiops_mock_fallback_enabled:
            payload = adapter_not_configured(
                "alertmanager",
                required_config="ALERTMANAGER_BASE_URL",
                summary_prefix="告警查询不可用",
                service_name=service_name,
                state=state,
            )
            payload.update({"alerts": []})
            return payload

        return self._mock_alerts(service_name)

    @staticmethod
    def _mock_alerts(service_name: str) -> dict[str, Any]:
        alerts = [
            {
                "alertname": "HighErrorRate",
                "service_name": service_name,
                "severity": "critical",
                "state": "active",
                "summary": f"{service_name} 5xx 错误率超过阈值",
            }
        ]
        return {
            "status": "success",
            "source": "mock",
            "service_name": service_name,
            "alerts": alerts,
            "signals": {"alert_count": len(alerts), "firing_count": len(alerts)},
            "summary": f"mock Alertmanager 返回 {len(alerts)} 条当前告警",
        }
