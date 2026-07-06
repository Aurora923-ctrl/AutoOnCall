"""FastAPI 应用入口

主应用程序，配置路由、中间件、静态文件等
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api import a2a, aiops, alerts, approvals, chat, evaluations, file, health, incidents
from app.config import config
from app.core.milvus_client import milvus_manager

# Used only to detect risky configuration values; this constant does not bind a socket.
EXTERNALLY_BOUND_HOSTS = {"0.0.0.0", "::", "[::]"}  # nosec B104


def production_exposure_warnings() -> list[str]:
    """Return warnings for demo defaults that are risky on externally bound hosts."""

    host = str(config.host).strip()
    externally_bound = host in EXTERNALLY_BOUND_HOSTS
    if not externally_bound:
        return []

    warnings: list[str] = []
    if not config.api_auth_enabled:
        warnings.append("API auth is disabled while binding to a non-local host")
    if "*" in config.cors_origins:
        warnings.append("CORS allows all origins while binding to a non-local host")
    if config.aiops_mock_fallback_enabled:
        warnings.append("AIOps mock fallback is enabled while binding to a non-local host")
    return warnings


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
    logger.info("=" * 60)
    logger.info(f"🚀 {config.app_name} v{config.app_version} 启动中...")
    logger.info(f"📝 环境: {'开发' if config.debug else '生产'}")
    logger.info(f"🌐 监听地址: http://{config.host}:{config.port}")
    logger.info(f"📚 API 文档: http://{config.host}:{config.port}/docs")

    enforce_production_exposure_policy()

    logger.info("🔌 Milvus 将在 readiness、RAG 检索或文档索引首次使用时按需连接")

    logger.info("=" * 60)

    yield

    logger.info("🔌 正在关闭 Milvus 连接...")
    milvus_manager.close()
    logger.info(f"👋 {config.app_name} 关闭")


app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="基于 LangChain 的智能oncall运维系统",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_credentials=True,
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
app.include_router(a2a.discovery_router, tags=["A2A Agent"])
app.include_router(a2a.router, prefix=config.normalized_a2a_base_path, tags=["A2A Agent"])

static_dir = "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    """返回首页"""
    index_path = os.path.join(static_dir, "index.html")

    if os.path.exists(index_path):
        return FileResponse(index_path)

    return {
        "message": f"Welcome to {config.app_name} API",
        "version": config.app_version,
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app", host=config.host, port=config.port, reload=config.debug, log_level="info"
    )
