"""Health check API."""

from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from loguru import logger

from app.config import config
from app.core.milvus_client import milvus_manager
from app.integrations.base import bearer_headers, classify_adapter_error
from app.integrations.mysql import MySQLStatusAdapter
from app.integrations.redis_info import RedisInfoAdapter
from app.utils.public_errors import public_adapter_error_message

router = APIRouter()


@router.get("/health")
async def health_check():
    """Compatibility endpoint with readiness semantics."""
    return await readiness_check()


@router.get("/health/live")
async def liveness_check():
    """Return process liveness without checking external dependencies."""
    health_data = _base_health_data()
    health_data["status"] = "healthy"
    health_data["checks"] = {
        "process": {
            "status": "alive",
            "message": "FastAPI process is responsive",
        }
    }
    return JSONResponse(
        status_code=200,
        content={
            "code": 200,
            "message": "service process is alive",
            "data": health_data,
        },
    )


@router.get("/health/ready")
async def readiness_check():
    """Return dependency readiness for all production traffic capabilities."""
    health_data = await _dependency_health_data()

    unready_capabilities = [
        name
        for name, capability in health_data.get("capabilities", {}).items()
        if not capability.get("ready")
    ]
    status_code = 200
    if unready_capabilities:
        status_code = 503
        health_data["error"] = "Readiness dependencies unavailable: " + ", ".join(
            unready_capabilities
        )

    health_data["status"] = "healthy" if status_code == 200 else "degraded"
    health_data["unready_capabilities"] = unready_capabilities
    return JSONResponse(
        status_code=status_code,
        content={
            "code": status_code,
            "message": (
                "service dependencies are ready"
                if status_code == 200
                else "service dependency is unavailable"
            ),
            "data": health_data,
        },
    )


@router.get("/health/ready/rag")
async def rag_readiness_check():
    """Return readiness for RAG search and upload indexing."""
    health_data = await _dependency_health_data()
    rag_ready = bool(health_data["capabilities"]["rag"]["ready"])
    status_code = 200 if rag_ready else 503
    health_data["selected_capability"] = "rag"
    health_data["status"] = "healthy" if rag_ready else "degraded"
    if not rag_ready:
        health_data["error"] = "RAG readiness dependency unavailable"
    return JSONResponse(
        status_code=status_code,
        content={
            "code": status_code,
            "message": (
                "RAG capability is ready" if rag_ready else "RAG capability is unavailable"
            ),
            "data": health_data,
        },
    )


@router.get("/health/ready/aiops")
async def aiops_readiness_check():
    """Return readiness for AIOps diagnosis."""
    health_data = await _dependency_health_data()
    aiops_ready = bool(health_data["capabilities"]["aiops"]["ready"])
    status_code = 200 if aiops_ready else 503
    health_data["selected_capability"] = "aiops"
    health_data["status"] = "healthy" if aiops_ready else "degraded"
    if not aiops_ready:
        health_data["error"] = "AIOps readiness dependency unavailable"
    return JSONResponse(
        status_code=status_code,
        content={
            "code": status_code,
            "message": (
                "AIOps capability is ready" if aiops_ready else "AIOps capability is unavailable"
            ),
            "data": health_data,
        },
    )


async def _dependency_health_data() -> dict[str, Any]:
    """Build the shared dependency view used by capability-specific probes."""
    health_data = _base_health_data()
    milvus = _check_milvus()
    external_systems = await _external_system_readiness()
    health_data["checks"] = {
        "process": {
            "status": "alive",
            "message": "FastAPI process is responsive",
        },
        "milvus": milvus,
        "external_systems": external_systems,
    }
    health_data["capabilities"] = _capability_readiness(milvus, external_systems)
    health_data["milvus"] = milvus

    return health_data


def _base_health_data() -> dict[str, Any]:
    return {
        "service": config.app_name,
        "version": config.app_version,
        "status": "healthy",
        "mode": "production",
    }


def _check_milvus() -> dict[str, str]:
    try:
        if not milvus_manager.health_check():
            try:
                _ = milvus_manager.connect()
            except Exception as exc:
                logger.warning(f"Milvus readiness connection failed: {exc}")
                return {
                    "status": "disconnected",
                    "error_type": classify_adapter_error(exc),
                    "message": "Milvus disconnected",
                }

        milvus_healthy = milvus_manager.health_check()
        return {
            "status": "connected" if milvus_healthy else "disconnected",
            "message": "Milvus connected" if milvus_healthy else "Milvus disconnected",
        }
    except Exception as exc:
        logger.warning(f"Milvus health check failed: {exc}")
        return {
            "status": "error",
            "error_type": classify_adapter_error(exc),
            "message": "Milvus check failed",
        }


async def _external_system_readiness() -> dict[str, Any]:
    checks = {
        "prometheus": await _http_get_readiness(
            base_url=config.prometheus_base_url,
            path="/-/ready",
            token=config.prometheus_bearer_token,
            timeout_seconds=config.prometheus_timeout_seconds,
        ),
        "log_gateway": _configured_only_check(bool(config.log_gateway_url)),
        "kubernetes": await _http_get_readiness(
            base_url=config.kubernetes_api_server,
            path="/version",
            token=config.kubernetes_bearer_token,
            timeout_seconds=config.kubernetes_timeout_seconds,
            verify=config.kubernetes_verify_ssl,
        ),
        "redis": await _redis_readiness(),
        "mysql": await _mysql_readiness(),
        "ticket": _configured_only_check(bool(config.ticket_api_url or config.resolved_mysql_dsn)),
    }
    statuses = {name: payload["status"] for name, payload in checks.items()}
    configured = {name: status != "not_configured" for name, status in statuses.items()}
    return {
        "status": _external_overall_status(statuses),
        "checks": checks,
        "configured": configured,
        "mock_fallback_enabled": False,
        "message": "Unconfigured external systems will return structured failures",
    }


def _configured_only_check(configured: bool) -> dict[str, Any]:
    return {
        "status": "configured" if configured else "not_configured",
        "configured": configured,
    }


async def _http_get_readiness(
    *,
    base_url: str,
    path: str,
    token: str = "",
    timeout_seconds: float = 5.0,
    verify: bool = True,
) -> dict[str, Any]:
    if not base_url:
        return {"status": "not_configured", "configured": False}

    normalized_base = base_url.rstrip("/")
    probe_path = path if path.startswith("/") else f"/{path}"
    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            headers=bearer_headers(token),
            verify=verify,
        ) as client:
            response = await client.get(f"{normalized_base}{probe_path}")
            response.raise_for_status()
    except Exception as exc:
        return _failed_readiness(exc)

    return {
        "status": "connected",
        "configured": True,
        "probe": probe_path,
        "message": "external dependency is reachable",
    }


async def _redis_readiness() -> dict[str, Any]:
    adapter = RedisInfoAdapter()
    if not adapter.configured:
        return {"status": "not_configured", "configured": False}
    try:
        result = await adapter.ping()
        return {"status": "connected", "configured": True, **result}
    except Exception as exc:
        return _failed_readiness(exc)


async def _mysql_readiness() -> dict[str, Any]:
    adapter = MySQLStatusAdapter()
    if not adapter.configured:
        return {"status": "not_configured", "configured": False}
    try:
        result = await adapter.ping()
        return {"status": "connected", "configured": True, **result}
    except Exception as exc:
        return _failed_readiness(exc)


def _failed_readiness(exc: Exception) -> dict[str, Any]:
    return {
        "status": "failed",
        "configured": True,
        "error_type": classify_adapter_error(exc),
        "message": public_adapter_error_message(exc),
    }


def _external_overall_status(statuses: dict[str, str]) -> str:
    if any(status == "failed" for status in statuses.values()):
        return "degraded"
    if any(status in {"connected", "configured"} for status in statuses.values()):
        return "configured"
    return "not_configured"


def _capability_readiness(
    milvus: dict[str, str],
    external_systems: dict[str, Any],
) -> dict[str, Any]:
    rag_ready = milvus.get("status") == "connected"
    external_status = str(external_systems.get("status") or "unknown")
    if external_status == "configured":
        aiops_status = external_status
        aiops_ready = True
    else:
        aiops_status = external_status
        aiops_ready = False

    return {
        "rag": {
            "ready": rag_ready,
            "status": "ready" if rag_ready else "unavailable",
            "dependency": "milvus",
            "message": (
                "RAG search and upload indexing are ready"
                if rag_ready
                else "RAG search and upload indexing need Milvus"
            ),
        },
        "aiops": {
            "ready": aiops_ready,
            "status": aiops_status,
            "mock_fallback_enabled": False,
            "message": (
                "AIOps diagnosis can use configured adapters"
                if aiops_ready
                else "AIOps diagnosis has no configured adapters"
            ),
        },
    }
