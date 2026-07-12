from __future__ import annotations

import httpx
import pytest

from app.integrations.service_catalog import CMDBAdapter, DeployHistoryAdapter
from app.integrations.ticketing import TicketingAdapter


def _json_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


@pytest.mark.asyncio
async def test_cmdb_adapter_preserves_business_context_fields() -> None:
    transport = httpx.MockTransport(
        lambda _request: _json_response(
            {
                "service_name": "order-service",
                "owner": "payments-oncall",
                "tier": "critical",
                "namespace": "orders",
                "business_context": {
                    "critical_user_journey": "submit cart -> create order",
                    "impact_when_degraded": "customers cannot submit new orders",
                },
                "critical_endpoints": [{"path": "POST /api/orders"}],
                "slo": {"p95_latency_ms": 900},
                "dependencies": [{"name": "redis-order-cache", "type": "redis"}],
            }
        )
    )
    adapter = CMDBAdapter(url="http://cmdb", transport=transport)

    payload = await adapter.query_service("order-service")

    assert payload["source"] == "cmdb"
    assert payload["tier"] == "critical"
    assert payload["business_context"]["impact_when_degraded"].startswith("customers")
    assert payload["critical_endpoints"][0]["path"] == "POST /api/orders"
    assert payload["slo"]["p95_latency_ms"] == 900
    assert payload["signals"]["has_business_context"] is True


@pytest.mark.asyncio
async def test_deploy_history_adapter_exposes_change_risk_and_version() -> None:
    transport = httpx.MockTransport(
        lambda _request: _json_response(
            {
                "service_name": "inventory-service",
                "current_version": "2026.06.27-0730",
                "recent_deployments": [
                    {
                        "change_id": "CHG-10088",
                        "status": "succeeded",
                        "risk": "high",
                        "summary": "ConfigMap refresh",
                    }
                ],
            }
        )
    )
    adapter = DeployHistoryAdapter(url="http://deploy-history", transport=transport)

    payload = await adapter.query_deployments("inventory-service")

    assert payload["source"] == "deploy_history"
    assert payload["current_version"] == "2026.06.27-0730"
    assert payload["recent_change"]["change_id"] == "CHG-10088"
    assert payload["signals"]["high_risk_change_count"] == 1
    assert payload["high_risk_changes"][0]["summary"] == "ConfigMap refresh"


@pytest.mark.asyncio
async def test_payment_deploy_history_marks_report_feature_flag_as_supporting_context() -> None:
    transport = httpx.MockTransport(
        lambda _request: _json_response(
            {
                "service_name": "payment-service",
                "current_version": "2026.06.27-0910",
                "recent_deployments": [
                    {
                        "change_id": "CHG-10087",
                        "status": "succeeded",
                        "risk": "medium",
                        "summary": "Enabled payment reconciliation report with a new date-range query.",
                        "related_config": ["PAYMENT_REPORT_ENABLED=true"],
                    }
                ],
            }
        )
    )
    adapter = DeployHistoryAdapter(url="http://deploy-history", transport=transport)

    payload = await adapter.query_deployments("payment-service")

    assert payload["signals"]["feature_flag_change"] is True
    assert payload["release_correlation"]["change_id"] == "CHG-10087"
    assert payload["release_correlation"]["root_cause_role"] == "supporting_correlation"
    assert "cannot prove root cause" in payload["uncertainty"]


@pytest.mark.asyncio
async def test_ticketing_adapter_filters_and_preserves_replay_context() -> None:
    transport = httpx.MockTransport(
        lambda _request: _json_response(
            {
                "items": [
                    {
                        "ticket_id": "INC-REDIS-001",
                        "service_name": "order-service",
                        "title": "Redis maxclients exhausted",
                        "root_cause": "Redis connected_clients reached maxclients",
                        "resolution": "Cap client pools",
                        "customer_impact": "Checkout order creation returned 503",
                        "evidence": ["Redis connected_clients was 9940/10000"],
                        "timeline": [{"time": "20:07", "event": "evidence collected"}],
                        "prevention": ["Add early warning"],
                        "labels": ["redis", "maxclients"],
                    },
                    {
                        "ticket_id": "INC-MYSQL-001",
                        "service_name": "payment-service",
                        "title": "MySQL slow query",
                    },
                ]
            }
        )
    )
    adapter = TicketingAdapter(url="http://tickets", transport=transport)

    payload = await adapter.search_history("order-service", query="redis timeout", limit=5)

    assert payload["source"] == "ticket_api"
    assert payload["signals"]["ticket_count"] == 1
    ticket = payload["tickets"][0]
    assert ticket["ticket_id"] == "INC-REDIS-001"
    assert ticket["customer_impact"].startswith("Checkout")
    assert ticket["evidence"] == ["Redis connected_clients was 9940/10000"]
    assert ticket["timeline"][0]["event"] == "evidence collected"
    assert ticket["prevention"] == ["Add early warning"]
