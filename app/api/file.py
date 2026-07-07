"""文件上传接口模块"""

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger

from app.config import config
from app.core.auth import KNOWLEDGE_WRITE_SCOPE, READ_SCOPE, require_scope
from app.models.api_contracts import KnowledgeIndexingReportsResponse
from app.services.indexing_quality_service import indexing_quality_service
from app.services.vector_index_service import vector_index_service

router = APIRouter()

UPLOAD_DIR = Path(config.upload_dir)

ALLOWED_EXTENSIONS = config.upload_allowed_extension_list

MAX_FILE_SIZE_MB = config.upload_max_file_size_mb
MAX_FILE_SIZE = config.upload_max_file_size
UPLOAD_READ_CHUNK_SIZE = config.upload_read_chunk_size
PUBLIC_UPLOAD_ERROR_MESSAGE = "文件上传失败，请稍后重试"
PUBLIC_INDEXING_ERROR_MESSAGE = "向量索引失败，请检查服务端日志"
PUBLIC_DIRECTORY_INDEX_ERROR_MESSAGE = "索引目录失败，请检查服务端日志"
MAX_SAFE_FILENAME_LENGTH = 160
WINDOWS_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@router.get("/upload/config", dependencies=[Depends(require_scope(READ_SCOPE))])
async def upload_config():
    """Return upload constraints used by the frontend before selecting a file."""
    return {
        "code": 200,
        "message": "success",
        "data": {
            "allowed_extensions": ALLOWED_EXTENSIONS,
            "max_file_size": MAX_FILE_SIZE,
            "max_file_size_mb": MAX_FILE_SIZE_MB,
        },
    }


@router.post("/upload", dependencies=[Depends(require_scope(KNOWLEDGE_WRITE_SCOPE))])
async def upload_file(file: UploadFile = File(...)):
    """
    上传文件并自动创建向量索引

    Args:
        file: 上传的文件

    Returns:
        JSONResponse: 上传结果
    """
    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="文件名不能为空")

        safe_filename = _sanitize_filename(file.filename)
        _validate_safe_filename(safe_filename)

        file_extension = _get_file_extension(safe_filename)
        if file_extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式，仅支持: {', '.join(ALLOWED_EXTENSIONS)}",
            )

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        file_path = UPLOAD_DIR / safe_filename
        overwritten = file_path.exists()

        if overwritten:
            logger.info(f"文件已存在，将覆盖: {file_path}")
        uploaded_size = await _save_upload_file(file, file_path)

        logger.info(f"文件上传成功: {file_path}")

        # 5. 自动创建向量索引
        indexing_status: dict[str, Any] = {
            "status": "success",
            "chunk_count": 0,
            "duration_ms": 0,
            "error_message": None,
            "message": None,
        }
        try:
            logger.info(f"开始为上传文件创建向量索引: {file_path}")
            indexing_result = await asyncio.to_thread(
                vector_index_service.index_single_file,
                str(file_path),
            )
            if hasattr(indexing_result, "to_dict"):
                indexing_status = indexing_result.to_dict()
            elif isinstance(indexing_result, dict):
                indexing_status = indexing_result
            if hasattr(indexing_result, "cleaning_report"):
                indexing_quality_service.record_single_file_result(
                    indexing_result,
                    operation="upload",
                    source_path=str(file_path),
                )

            if indexing_status.get("status") == "empty":
                logger.warning(f"上传文件未产生可检索分片: {file_path}")
            else:
                logger.info(f"向量索引创建成功: {file_path}")
        except Exception:
            logger.exception(f"向量索引创建失败: {file_path}")
            indexing_status = {
                "status": "failed",
                "chunk_count": 0,
                "duration_ms": 0,
                "error_message": PUBLIC_INDEXING_ERROR_MESSAGE,
                "message": "文件已保存，但索引未完成",
                "cleaning": {},
            }
            indexing_quality_service.record_failed_file(
                source_path=str(file_path),
                operation="upload",
                error_message=PUBLIC_INDEXING_ERROR_MESSAGE,
            )
            # 注意：即使索引失败，文件上传仍然成功，但响应会暴露索引阶段状态。

        indexing_ready = indexing_status.get("status") == "success"
        response_status = 200 if indexing_ready else 207
        response_message = "success" if indexing_ready else "partial_success"

        return JSONResponse(
            status_code=response_status,
            content={
                "code": response_status,
                "message": response_message,
                "data": {
                    "filename": safe_filename,
                    "file_path": safe_filename,
                    "size": uploaded_size,
                    "overwritten": overwritten,
                    "indexing_ready": indexing_ready,
                    "indexing": indexing_status,
                },
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"文件上传失败: {e}")
        raise HTTPException(status_code=500, detail=PUBLIC_UPLOAD_ERROR_MESSAGE) from e


@router.post(
    "/index_directory",
    dependencies=[Depends(require_scope(KNOWLEDGE_WRITE_SCOPE))],
    summary="批量索引目录（运维入口）",
    description="面向 make upload 等批处理/运维流程；前端工作台主入口仍使用 /upload。",
)
async def index_directory(directory_path: str | None = None):
    """
    批量索引指定目录下的所有文件。

    这是运维/批处理入口，不属于前端工作台的主上传链路。

    Args:
        directory_path: 目录路径（可选，默认使用 uploads 目录）

    Returns:
        JSONResponse: 索引结果
    """
    try:
        logger.info(f"开始索引目录: {directory_path or 'uploads'}")

        result = await asyncio.to_thread(vector_index_service.index_directory, directory_path)
        indexing_quality_service.record_directory_result(result, operation="directory")
        response_status = _index_directory_response_status(result)

        return JSONResponse(
            status_code=response_status,
            content={
                "code": response_status,
                "message": "success" if result.success else "partial_success",
                "data": result.to_public_dict(),
            },
        )

    except Exception as e:
        logger.exception(f"索引目录失败: {e}")
        raise HTTPException(
            status_code=500,
            detail=PUBLIC_DIRECTORY_INDEX_ERROR_MESSAGE,
        ) from e


@router.get(
    "/knowledge/indexing/reports",
    response_model=KnowledgeIndexingReportsResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def knowledge_indexing_reports(
    doc_type: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return persisted loader cleaning quality reports and doc_type aggregates."""
    safe_limit = max(1, min(int(limit), 500))
    return {
        "code": 200,
        "message": "success",
        "data": indexing_quality_service.build_report(
            doc_type=doc_type,
            limit=safe_limit,
        ),
    }


def _get_file_extension(filename: str) -> str:
    """
    获取文件扩展名

    Args:
        filename: 文件名

    Returns:
        str: 扩展名（小写，不含点）
    """
    parts = filename.rsplit(".", 1)
    if len(parts) == 2:
        return parts[1].lower()
    return ""


def _index_directory_response_status(result: Any) -> int:
    """Map directory indexing results to HTTP status codes."""
    if getattr(result, "success", False):
        return 200
    error_type = getattr(result, "error_type", "")
    if error_type == "forbidden_directory":
        return 403
    if error_type == "invalid_directory":
        return 400
    return 207


async def _save_upload_file(file: UploadFile, file_path: Path) -> int:
    """Write an uploaded file in bounded chunks, then replace the target path."""
    temp_path = file_path.with_name(f".{file_path.name}.{uuid4().hex}.tmp")
    uploaded_size = 0

    try:
        with temp_path.open("wb") as handle:
            while True:
                chunk = await file.read(UPLOAD_READ_CHUNK_SIZE)
                if not chunk:
                    break
                uploaded_size += len(chunk)
                if uploaded_size > MAX_FILE_SIZE:
                    raise HTTPException(
                        status_code=400,
                        detail=f"文件大小超过限制（最大 {MAX_FILE_SIZE_MB}MB）",
                    )
                handle.write(chunk)

        temp_path.replace(file_path)
        return uploaded_size
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(f"清理临时上传文件失败: {temp_path}")
        raise


def _validate_safe_filename(filename: str) -> None:
    """Validate that a sanitized filename still has a useful basename."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    normalized_stem = stem.replace("_", "").replace(".", "").strip()
    if not filename or not normalized_stem:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    if len(filename) > MAX_SAFE_FILENAME_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"文件名过长（最大 {MAX_SAFE_FILENAME_LENGTH} 字符）",
        )
    if stem.upper() in WINDOWS_RESERVED_FILENAMES:
        raise HTTPException(status_code=400, detail="文件名不能使用系统保留名称")


def _sanitize_filename(filename: str) -> str:
    """
    规范化文件名，去除空格和特殊字符

    Args:
        filename: 原始文件名

    Returns:
        str: 规范化后的文件名
    """
    # 去除首尾空白并替换中间空格
    sanitized = filename.strip().replace(" ", "_")
    for char in ["\\", "/", ":", "*", "?", '"', "<", ">", "|"]:
        sanitized = sanitized.replace(char, "_")
    sanitized = "".join("_" if ord(char) < 32 else char for char in sanitized)
    return sanitized
