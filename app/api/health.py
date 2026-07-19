"""Health check API."""

import asyncio
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
from app.models.response import HealthApiResponse
from app.utils.public_errors import public_adapter_error_message

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthApiResponse,
    responses={503: {"model": HealthApiResponse}},
)
async def health_check():
    """Compatibility endpoint with readiness semantics."""
    return await readiness_check()


@router.get("/health/live", response_model=HealthApiResponse)
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


@router.get(
    "/health/ready",
    response_model=HealthApiResponse,
    responses={503: {"model": HealthApiResponse}},
)
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


@router.get(
    "/health/ready/rag",
    response_model=HealthApiResponse,
    responses={503: {"model": HealthApiResponse}},
)
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


@router.get(
    "/health/ready/aiops",
    response_model=HealthApiResponse,
    responses={503: {"model": HealthApiResponse}},
)
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
    milvus, external_systems = await asyncio.gather(
        asyncio.to_thread(_check_milvus),
        _external_system_readiness(),
    )
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
        "mode": "development" if config.debug else "production",
    }


def _check_milvus() -> dict[str, str]:
    try:
        milvus_healthy = milvus_manager.readiness_check()
        payload = {
            "status": "connected" if milvus_healthy else "disconnected",
            "message": "Milvus connected" if milvus_healthy else "Milvus disconnected",
        }
        if milvus_healthy and not _embedding_configuration_ready():
            return {
                "status": "partial",
                "message": "Milvus connected but embedding configuration is incomplete",
            }
        return payload
    except Exception as exc:
        logger.warning(
            "Milvus health check failed: error_type={}",
            classify_adapter_error(exc),
        )
        return {
            "status": "error",
            "error_type": classify_adapter_error(exc),
            "message": "Milvus check failed",
        }


async def _external_system_readiness() -> dict[str, Any]:
    try:
        prometheus, loki, kubernetes, redis, mysql = await asyncio.gather(
            _http_get_readiness(
                base_url=config.prometheus_base_url,
                path="/-/ready",
                token=config.prometheus_bearer_token,
                timeout_seconds=config.prometheus_timeout_seconds,
            ),
            _http_get_readiness(
                base_url=config.loki_base_url,
                path="/ready",
                token=config.loki_bearer_token,
                timeout_seconds=config.loki_timeout_seconds,
            ),
            _http_get_readiness(
                base_url=config.kubernetes_api_server,
                path="/version",
                token=config.kubernetes_bearer_token,
                timeout_seconds=config.kubernetes_timeout_seconds,
                verify=config.kubernetes_verify_ssl,
            ),
            _redis_readiness(),
            _mysql_readiness(),
        )
    except Exception as exc:
        logger.warning(
            "External readiness aggregation failed: error_type={}",
            classify_adapter_error(exc),
        )
        return {
            "status": "degraded",
            "checks": {},
            "configured": {},
            "mock_fallback_enabled": config.aiops_mock_fallback_enabled,
            "message": "External dependency readiness checks failed",
        }
    checks = {
        "prometheus": prometheus,
        "loki": loki,
        "log_gateway": _configured_only_check(bool(config.log_gateway_url)),
        "kubernetes": kubernetes,
        "redis": redis,
        "mysql": mysql,
        "ticket": _configured_only_check(bool(config.ticket_api_url or config.resolved_mysql_dsn)),
    }
    statuses = {name: payload["status"] for name, payload in checks.items()}
    configured = {name: status != "not_configured" for name, status in statuses.items()}
    return {
        "status": _external_overall_status(statuses),
        "checks": checks,
        "configured": configured,
        "mock_fallback_enabled": config.aiops_mock_fallback_enabled,
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
    try:
        adapter = RedisInfoAdapter()
        if not adapter.configured:
            return {"status": "not_configured", "configured": False}
        result = await adapter.ping()
        return _connected_readiness(result)
    except Exception as exc:
        return _failed_readiness(exc)


async def _mysql_readiness() -> dict[str, Any]:
    try:
        adapter = MySQLStatusAdapter()
        if not adapter.configured:
            return {"status": "not_configured", "configured": False}
        result = await adapter.ping()
        return _connected_readiness(result)
    except Exception as exc:
        return _failed_readiness(exc)


def _connected_readiness(result: dict[str, Any]) -> dict[str, Any]:
    """Return a public readiness result without exposing dependency endpoints."""
    return {
        "status": "connected",
        "configured": True,
        "message": str(result.get("message") or "external dependency is reachable"),
    }


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
    if any(status == "connected" for status in statuses.values()):
        return "configured"
    if any(status == "configured" for status in statuses.values()):
        return "unverified"
    return "not_configured"


def _capability_readiness(
    milvus: dict[str, str],
    external_systems: dict[str, Any],
) -> dict[str, Any]:
    rag_ready = milvus.get("status") == "connected"
    external_status = str(external_systems.get("status") or "unknown")
    checks = external_systems.get("checks")
    check_map = checks if isinstance(checks, dict) else {}
    observability_ready = any(
        _readiness_status(check_map, source) == "connected" for source in ("prometheus", "loki")
    )
    configured_failure = any(
        isinstance(payload, dict)
        and payload.get("configured") is True
        and payload.get("status") == "failed"
        for payload in check_map.values()
    )
    aiops_ready = external_status == "configured" and observability_ready and not configured_failure
    if configured_failure:
        aiops_status = "degraded"
    elif external_status == "configured" and not observability_ready:
        aiops_status = "partial"
    else:
        aiops_status = external_status

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
            "required_dependency": "prometheus_or_loki",
            "mock_fallback_enabled": bool(external_systems.get("mock_fallback_enabled")),
            "message": (
                "AIOps diagnosis has a verified observability source"
                if aiops_ready
                else "AIOps diagnosis needs reachable Prometheus or Loki without adapter failures"
            ),
        },
    }


def _embedding_configuration_ready() -> bool:
    """Return whether indexing and query embedding can be attempted."""
    return bool(
        str(config.dashscope_api_key or "").strip()
        and str(config.dashscope_embedding_model or "").strip()
        and int(config.dashscope_embedding_dimensions) > 0
    )


def _readiness_status(checks: dict[str, Any], name: str) -> str:
    payload = checks.get(name)
    if not isinstance(payload, dict):
        return "unknown"
    return str(payload.get("status") or "unknown")
