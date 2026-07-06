"""Tests for liveness and readiness health checks."""

import json

import pytest

from app.api import health as health_api


@pytest.mark.asyncio
async def test_liveness_does_not_check_milvus(monkeypatch) -> None:
    def fail_health_check() -> bool:
        raise RuntimeError("milvus should not be checked")

    monkeypatch.setattr(health_api.milvus_manager, "health_check", fail_health_check)

    response = await health_api.liveness_check()
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert payload["data"]["checks"]["process"]["status"] == "alive"


@pytest.mark.asyncio
async def test_readiness_reports_milvus_dependency_failure(monkeypatch) -> None:
    monkeypatch.setattr(health_api.config, "aiops_mock_fallback_enabled", True)
    monkeypatch.setattr(health_api.milvus_manager, "health_check", lambda: False)

    def fail_connect():
        raise RuntimeError("milvus down")

    monkeypatch.setattr(health_api.milvus_manager, "connect", fail_connect)

    response = await health_api.readiness_check()
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 503
    assert payload["data"]["status"] == "degraded"
    assert payload["data"]["checks"]["milvus"]["status"] == "disconnected"
    assert payload["data"]["capabilities"]["rag"]["ready"] is False
    assert "aiops" in payload["data"]["capabilities"]
    assert payload["data"]["checks"]["external_systems"]["status"] in {
        "configured",
        "mock_fallback",
    }


@pytest.mark.asyncio
async def test_readiness_attempts_lazy_milvus_connection(monkeypatch) -> None:
    async def external_ready():
        return {"status": "mock_fallback", "mock_fallback_enabled": True}

    class LazyMilvus:
        connected = False

        def health_check(self) -> bool:
            return self.connected

        def connect(self):
            self.connected = True
            return object()

    monkeypatch.setattr(health_api, "milvus_manager", LazyMilvus())
    monkeypatch.setattr(health_api, "_external_system_readiness", external_ready)

    response = await health_api.readiness_check()
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert payload["data"]["checks"]["milvus"]["status"] == "connected"
    assert payload["data"]["capabilities"]["rag"]["ready"] is True


@pytest.mark.asyncio
async def test_readiness_reports_aiops_dependency_failure(monkeypatch) -> None:
    async def external_not_ready():
        return {"status": "not_configured", "mock_fallback_enabled": False}

    monkeypatch.setattr(health_api.milvus_manager, "health_check", lambda: True)
    monkeypatch.setattr(health_api, "_external_system_readiness", external_not_ready)

    response = await health_api.readiness_check()
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 503
    assert payload["data"]["status"] == "degraded"
    assert payload["data"]["capabilities"]["rag"]["ready"] is True
    assert payload["data"]["capabilities"]["aiops"]["ready"] is False
    assert payload["data"]["unready_capabilities"] == ["aiops"]


@pytest.mark.asyncio
async def test_capability_readiness_splits_aiops_from_rag(monkeypatch) -> None:
    monkeypatch.setattr(health_api.config, "aiops_mock_fallback_enabled", True)
    monkeypatch.setattr(health_api.milvus_manager, "health_check", lambda: False)

    def fail_connect():
        raise RuntimeError("milvus down")

    monkeypatch.setattr(health_api.milvus_manager, "connect", fail_connect)

    response = await health_api.aiops_readiness_check()
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert payload["data"]["selected_capability"] == "aiops"
    assert payload["data"]["checks"]["milvus"]["status"] == "disconnected"
    assert payload["data"]["capabilities"]["rag"]["ready"] is False
    assert payload["data"]["capabilities"]["aiops"]["ready"] is True


@pytest.mark.asyncio
async def test_rag_readiness_uses_rag_dependency_status(monkeypatch) -> None:
    monkeypatch.setattr(health_api.milvus_manager, "health_check", lambda: False)

    def fail_connect():
        raise RuntimeError("milvus down")

    monkeypatch.setattr(health_api.milvus_manager, "connect", fail_connect)

    response = await health_api.rag_readiness_check()
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 503
    assert payload["data"]["selected_capability"] == "rag"
    assert payload["data"]["capabilities"]["rag"]["ready"] is False


@pytest.mark.asyncio
async def test_health_keeps_readiness_compatible(monkeypatch) -> None:
    async def external_ready():
        return {"status": "mock_fallback", "mock_fallback_enabled": True}

    monkeypatch.setattr(health_api.milvus_manager, "health_check", lambda: True)
    monkeypatch.setattr(health_api, "_external_system_readiness", external_ready)

    response = await health_api.health_check()
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert payload["data"]["checks"]["milvus"]["status"] == "connected"


@pytest.mark.asyncio
async def test_readiness_reports_external_connected_and_failed(monkeypatch) -> None:
    monkeypatch.setattr(health_api.config, "aiops_mock_fallback_enabled", True)
    monkeypatch.setattr(health_api.milvus_manager, "health_check", lambda: True)

    class ConnectedRedis:
        configured = True

        async def ping(self):
            return {"status": "connected", "message": "PONG"}

    class FailedMySQL:
        configured = True

        async def ping(self):
            raise ConnectionError("mysql down")

    monkeypatch.setattr(health_api, "RedisInfoAdapter", lambda: ConnectedRedis())
    monkeypatch.setattr(health_api, "MySQLStatusAdapter", lambda: FailedMySQL())

    response = await health_api.readiness_check()
    payload = json.loads(response.body.decode("utf-8"))
    external = payload["data"]["checks"]["external_systems"]

    assert response.status_code == 200
    assert external["status"] == "degraded"
    assert external["checks"]["redis"]["status"] == "connected"
    assert external["checks"]["mysql"]["status"] == "failed"
    assert payload["data"]["capabilities"]["aiops"]["status"] == "degraded_with_fallback"


def test_external_overall_status_respects_mock_fallback_flag() -> None:
    statuses = {"alertmanager": "not_configured", "prometheus": "not_configured"}

    assert health_api._external_overall_status(statuses, True) == "mock_fallback"
    assert health_api._external_overall_status(statuses, False) == "not_configured"
