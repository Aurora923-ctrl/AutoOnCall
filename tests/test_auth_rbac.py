"""API-token RBAC tests."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.api import aiops, approvals
from app.config import config


def _set_auth_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
    read_token: str = "",
    operator_token: str = "",
    approver_token: str = "",
    admin_token: str = "",
    auth_tokens: str = "",
) -> None:
    monkeypatch.setattr(config, "api_auth_enabled", enabled)
    monkeypatch.setattr(config, "api_read_token", read_token)
    monkeypatch.setattr(config, "api_operator_token", operator_token)
    monkeypatch.setattr(config, "api_approver_token", approver_token)
    monkeypatch.setattr(config, "api_admin_token", admin_token)
    monkeypatch.setattr(config, "api_auth_tokens", auth_tokens)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(aiops.router, prefix="/api")
    app.include_router(approvals.router, prefix="/api")
    return app


@pytest.mark.asyncio
async def test_auth_disabled_keeps_local_demo_routes_open(monkeypatch) -> None:
    _set_auth_config(monkeypatch, enabled=False)
    app = _build_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/aiops/tools/contracts")

    assert response.status_code == 200
    assert response.json()["count"] > 0


@pytest.mark.asyncio
async def test_auth_enabled_without_tokens_fails_closed(monkeypatch) -> None:
    _set_auth_config(monkeypatch, enabled=True)
    app = _build_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/aiops/tools/contracts")

    assert response.status_code == 503
    assert "no API tokens" in response.json()["detail"]


@pytest.mark.asyncio
async def test_read_token_can_read_but_cannot_approve_or_diagnose(monkeypatch) -> None:
    _set_auth_config(
        monkeypatch,
        enabled=True,
        read_token="read-secret",
        approver_token="approve-secret",
    )
    app = _build_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        missing = await client.get("/api/aiops/tools/contracts")
        readable = await client.get(
            "/api/aiops/tools/contracts",
            headers={"X-AutoOnCall-Token": "read-secret"},
        )
        diagnose = await client.post(
            "/api/aiops",
            headers={"X-AutoOnCall-Token": "read-secret"},
            json={"session_id": "auth-test"},
        )
        approve_with_reader = await client.post(
            "/api/incidents/inc-auth/approval",
            headers={"X-AutoOnCall-Token": "read-secret"},
            json={"decision": "approve", "decided_by": "pytest"},
        )
        approve_with_approver = await client.post(
            "/api/incidents/inc-auth/approval",
            headers={"Authorization": "Bearer approve-secret"},
            json={"decision": "approve", "decided_by": "pytest"},
        )

    assert missing.status_code == 401
    assert readable.status_code == 200
    assert diagnose.status_code == 403
    assert approve_with_reader.status_code == 403
    assert approve_with_approver.status_code == 404


@pytest.mark.asyncio
async def test_json_token_registry_expands_roles(monkeypatch) -> None:
    _set_auth_config(
        monkeypatch,
        enabled=True,
        auth_tokens='{"json-operator-token": {"name": "ops", "roles": ["operator"]}}',
    )
    app = _build_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/aiops/tools/contracts",
            headers={"Authorization": "Bearer json-operator-token"},
        )

    assert response.status_code == 200
