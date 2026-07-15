"""API-token RBAC tests."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.api import aiops, approvals, chat, evaluations, file as file_api
from app.config import config
from app.core.auth import authenticate_request
from app.models.approval import ApprovalRequest
from app.services.approval_service import ApprovalService


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
    app.include_router(chat.router, prefix="/api")
    app.include_router(evaluations.router, prefix="/api")
    app.include_router(file_api.router, prefix="/api")
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
        read_token="read-secret-token",
        approver_token="approve-secret-token",
    )
    app = _build_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        missing = await client.get("/api/aiops/tools/contracts")
        readable = await client.get(
            "/api/aiops/tools/contracts",
            headers={"X-AutoOnCall-Token": "read-secret-token"},
        )
        upload_config = await client.get(
            "/api/upload/config",
            headers={"X-AutoOnCall-Token": "read-secret-token"},
        )
        diagnose = await client.post(
            "/api/aiops",
            headers={"X-AutoOnCall-Token": "read-secret-token"},
            json={"session_id": "auth-test"},
        )
        approve_with_reader = await client.post(
            "/api/incidents/inc-auth/approval",
            headers={"X-AutoOnCall-Token": "read-secret-token"},
            json={"decision": "approve", "decided_by": "pytest"},
        )
        eval_with_reader = await client.get(
            "/api/eval/summary",
            headers={"X-AutoOnCall-Token": "read-secret-token"},
        )
        chat_with_reader = await client.post(
            "/api/chat",
            headers={"X-AutoOnCall-Token": "read-secret-token"},
            json={"Id": "auth-chat", "Question": "Redis timeout 怎么处理？"},
        )
        approve_with_approver = await client.post(
            "/api/incidents/inc-auth/approval",
            headers={"Authorization": "Bearer approve-secret-token"},
            json={"decision": "approve", "decided_by": "pytest"},
        )

    assert missing.status_code == 401
    assert readable.status_code == 200
    assert upload_config.status_code == 200
    assert diagnose.status_code == 403
    assert approve_with_reader.status_code == 403
    assert eval_with_reader.status_code == 403
    assert chat_with_reader.status_code == 403
    assert approve_with_approver.status_code == 404


@pytest.mark.asyncio
async def test_approver_token_is_used_as_approval_audit_actor(monkeypatch, tmp_path) -> None:
    _set_auth_config(monkeypatch, enabled=True, approver_token="approve-secret-token")
    service = ApprovalService(tmp_path / "approvals.db")
    request = service.create_request(
        ApprovalRequest(incident_id="inc-auth-audit", action="限流接口", risk_level="medium")
    )
    monkeypatch.setattr(approvals, "get_approval_service", lambda: service)
    app = _build_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/incidents/inc-auth-audit/approval",
            headers={"Authorization": "Bearer approve-secret-token"},
            json={
                "approval_id": request.approval_id,
                "decision": "approve",
                "decided_by": "spoofed-user",
            },
        )

    assert response.status_code == 200
    assert response.json()["approval"]["decided_by"] == "approver_token"


@pytest.mark.asyncio
async def test_json_token_registry_expands_roles(monkeypatch) -> None:
    _set_auth_config(
        monkeypatch,
        enabled=True,
        auth_tokens='{"json-operator-token-long": {"name": "ops", "roles": ["operator"]}}',
    )
    app = _build_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/aiops/tools/contracts",
            headers={"Authorization": "Bearer json-operator-token-long"},
        )

    assert response.status_code == 200


def test_json_token_registry_rejects_unsafe_audit_name(monkeypatch) -> None:
    _set_auth_config(
        monkeypatch,
        enabled=True,
        auth_tokens='{"json-reader-token-long": {"name": "ops\\nforged", "roles": ["viewer"]}}',
    )

    principal = authenticate_request(
        "read",
        x_autooncall_token="json-reader-token-long",
    )

    assert principal.token_name.startswith("json_token_")
    assert "\n" not in principal.token_name


@pytest.mark.asyncio
async def test_placeholder_and_short_tokens_are_not_accepted(monkeypatch) -> None:
    _set_auth_config(
        monkeypatch,
        enabled=True,
        read_token="replace-with-read-token",
        auth_tokens='{"short-token": ["viewer"]}',
    )
    app = _build_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/aiops/tools/contracts",
            headers={"Authorization": "Bearer replace-with-read-token"},
        )

    assert response.status_code == 503
    assert "no API tokens" in response.json()["detail"]
