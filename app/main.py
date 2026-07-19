"""FastAPI 应用入口

主应用程序，配置路由、中间件、静态文件等
"""

from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.agent.mcp_client import close_mcp_client
from app.api import (
    a2a,
    aiops,
    alerts,
    approvals,
    chat,
    evaluations,
    feedback,
    file,
    health,
    incidents,
)
from app.config import config
from app.core.auth import configured_token_scopes
from app.core.milvus_client import milvus_manager
from app.services.aiops_service import aiops_service
from app.services.vector_store_manager import vector_store_manager
from app.utils.log_safety import sanitize_log_value

# Used only to detect risky configuration values; this constant does not bind a socket.
EXTERNALLY_BOUND_HOSTS = {"0.0.0.0", "::", "[::]"}  # nosec B104
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "static"


def production_exposure_warnings() -> list[str]:
    """Return warnings for demo defaults that are risky on externally bound hosts."""

    if not is_externally_bound_host(config.host):
        return []

    warnings: list[str] = []
    if config.debug:
        warnings.append("debug mode is enabled while binding to a non-local host")
    if not config.api_auth_enabled:
        warnings.append("API auth is disabled while binding to a non-local host")
    elif not configured_token_scopes():
        warnings.append("API auth has no usable tokens while binding to a non-local host")
    if "*" in config.cors_origins:
        warnings.append("CORS allows all origins while binding to a non-local host")
    if config.aiops_mock_fallback_enabled:
        warnings.append("AIOps mock fallback is enabled while binding to a non-local host")
    return warnings


def is_externally_bound_host(host: object) -> bool:
    """Return whether a uvicorn bind host is reachable beyond loopback."""
    normalized = str(host or "").strip().lower().removeprefix("[").removesuffix("]")
    if normalized in {"localhost", "ip6-localhost"}:
        return False
    if normalized in EXTERNALLY_BOUND_HOSTS or not normalized:
        return True
    try:
        return not ip_address(normalized).is_loopback
    except ValueError:
        return True


def enforce_production_exposure_policy() -> None:
    """Warn or fail closed for unsafe production-facing demo defaults."""

    warnings = production_exposure_warnings()
    if warnings and config.production_exposure_strict:
        message = "Unsafe production exposure configuration: " + "; ".join(warnings)
        logger.error(message)
        raise RuntimeError(message)

    for warning in warnings:
        logger.warning(f"⚠️ 生产暴露配置提示: {warning}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    app_name = sanitize_log_value(config.app_name)
    app_version = sanitize_log_value(config.app_version)
    bind_host = sanitize_log_value(config.host)
    logger.info("=" * 60)
    logger.info(f"🚀 {app_name} v{app_version} 启动中...")
    logger.info(f"📝 环境: {'开发' if config.debug else '生产'}")
    logger.info(f"🌐 监听地址: http://{bind_host}:{config.port}")
    if config.debug:
        logger.info(f"📚 API 文档: http://{bind_host}:{config.port}/docs")

    enforce_production_exposure_policy()
    reconciled_runs = aiops_service.reconcile_incomplete_runs()
    if reconciled_runs:
        logger.warning(f"Reconciled {reconciled_runs} abandoned AIOps runs after startup")

    logger.info("🔌 Milvus 将在 readiness、RAG 检索或文档索引首次使用时按需连接")

    logger.info("=" * 60)

    try:
        yield
    finally:
        await close_mcp_client()
        logger.info("🔌 正在关闭 Milvus 连接...")
        try:
            await vector_store_manager.aclose()
        finally:
            milvus_manager.close()
        logger.info(f"👋 {app_name} 关闭")


app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="基于 LangChain 的智能oncall运维系统",
    lifespan=lifespan,
    docs_url="/docs" if config.debug else None,
    redoc_url="/redoc" if config.debug else None,
    openapi_url="/openapi.json" if config.debug else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, tags=["健康检查"])
app.include_router(chat.router, prefix="/api", tags=["对话"])
app.include_router(file.router, prefix="/api", tags=["文件管理"])
app.include_router(aiops.router, prefix="/api", tags=["AIOps智能运维"])
app.include_router(alerts.router, prefix="/api", tags=["AIOps告警接入"])
app.include_router(approvals.router, prefix="/api", tags=["AIOps人工审批"])
app.include_router(incidents.router, prefix="/api", tags=["AIOps故障事件"])
app.include_router(evaluations.router, prefix="/api", tags=["离线评测"])
app.include_router(feedback.router, prefix="/api", tags=["反馈闭环"])
app.include_router(a2a.discovery_router, tags=["A2A Agent"])
app.include_router(a2a.router, prefix=config.normalized_a2a_base_path, tags=["A2A Agent"])

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    """返回首页"""
    index_path = STATIC_DIR / "index.html"

    if index_path.exists():
        return FileResponse(index_path)

    response = {
        "message": f"Welcome to {config.app_name} API",
        "version": config.app_version,
    }
    if config.debug:
        response["docs"] = "/docs"
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app", host=config.host, port=config.port, reload=config.debug, log_level="info"
    )
