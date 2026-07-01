"""AIOps tool contract API tests."""

import httpx
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_aiops_tool_contracts_api_returns_read_only_contracts() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/aiops/tools/contracts")

    assert response.status_code == 200
    payload = response.json()
    names = {item["name"] for item in payload["items"]}
    redis_contract = next(item for item in payload["items"] if item["name"] == "query_redis_status")

    assert payload["count"] == len(payload["items"])
    assert "query_metrics" in names
    assert "search_runbook" in names
    assert redis_contract["read_only"] is True
    assert redis_contract["timeout_seconds"] > 0
    assert "data_sources" in redis_contract
    assert "degradation_strategy" in redis_contract
