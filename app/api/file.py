"""文件上传接口模块"""

import asyncio
import unicodedata
from pathlib import Path
from typing import Any
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from filelock import AsyncFileLock
from loguru import logger

from app.config import config
from app.core.auth import KNOWLEDGE_WRITE_SCOPE, READ_SCOPE, require_scope
from app.models.api_contracts import (
    KnowledgeIndexingReportsResponse,
    UploadConfigResponse,
    UploadFileResponse,
)
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
MAX_SAFE_FILENAME_BYTES = 200
WINDOWS_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
EXTENSION_MIME_TYPES = {
    "txt": {"text/plain"},
    "md": {"text/markdown", "text/plain"},
    "markdown": {"text/markdown", "text/plain"},
    "pdf": {"application/pdf", "application/x-pdf"},
    "html": {"text/html", "application/xhtml+xml"},
    "htm": {"text/html", "application/xhtml+xml"},
    "csv": {"text/csv", "application/csv", "application/vnd.ms-excel", "text/plain"},
    "xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/zip",
    },
}
GENERIC_UPLOAD_MIME_TYPES = {"", "application/octet-stream"}


@router.get(
    "/upload/config",
    response_model=UploadConfigResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def upload_config() -> dict[str, Any]:
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


@router.post(
    "/upload",
    response_model=UploadFileResponse,
    responses={207: {"model": UploadFileResponse}},
    dependencies=[Depends(require_scope(KNOWLEDGE_WRITE_SCOPE))],
)
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

        safe_filename = _validate_upload_filename(file.filename)

        file_extension = _get_file_extension(safe_filename)
        if file_extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式，仅支持: {', '.join(ALLOWED_EXTENSIONS)}",
            )
        _validate_upload_mime_type(file_extension, file.content_type)

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        file_path = UPLOAD_DIR / safe_filename
        async with AsyncFileLock(str(_upload_lock_path(file_path))):
            return await _save_and_index_upload(
                file=file,
                file_path=file_path,
                safe_filename=safe_filename,
                file_extension=file_extension,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"文件上传失败: {e}")
        raise HTTPException(status_code=500, detail=PUBLIC_UPLOAD_ERROR_MESSAGE) from e
    finally:
        await file.close()


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
        _record_directory_quality(result)
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


async def _save_and_index_upload(
    *,
    file: UploadFile,
    file_path: Path,
    safe_filename: str,
    file_extension: str,
) -> JSONResponse:
    """Save and index one file while the caller holds its per-path upload lock."""
    _cleanup_stale_upload_temps(file_path)
    overwritten = file_path.exists()
    if overwritten:
        logger.info(f"文件已存在，将覆盖: {file_path}")
    uploaded_size = await _save_upload_file(
        file,
        file_path,
        file_extension=file_extension,
    )

    logger.info(f"文件上传成功: {file_path}")

    indexing_status: dict[str, Any] = {
        "status": "failed",
        "chunk_count": 0,
        "duration_ms": 0,
        "error_message": PUBLIC_INDEXING_ERROR_MESSAGE,
        "message": "文件已保存，但索引未完成",
        "cleaning": {},
    }
    try:
        logger.info(f"开始为上传文件创建向量索引: {file_path}")
        indexing_result = await _run_index_single_file(file_path)
        indexing_status = _normalize_indexing_status(indexing_result)
        if hasattr(indexing_result, "cleaning_report"):
            _record_single_file_quality(
                indexing_result,
                source_path=str(file_path),
            )

        if indexing_status.get("status") == "success":
            logger.info(f"向量索引创建成功: {file_path}")
        elif indexing_status.get("status") == "empty":
            logger.warning(f"上传文件未产生可检索分片: {file_path}")
        else:
            logger.warning(f"上传文件索引未完成: {file_path}")
    except asyncio.CancelledError:
        logger.warning(f"上传请求已取消，索引任务完成后释放同名文件锁: {file_path}")
        raise
    except Exception:
        logger.exception(f"向量索引创建失败: {file_path}")
        _record_failed_file_quality(
            source_path=str(file_path),
        )

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


def _upload_lock_path(file_path: Path) -> Path:
    """Return the cross-process lock path protecting one upload and index transaction."""
    return file_path.with_name(f".{file_path.name}.upload.lock")


async def _run_index_single_file(file_path: Path) -> Any:
    """Keep the upload lock held until a worker-thread indexing call has really stopped."""
    indexing_task = asyncio.create_task(
        asyncio.to_thread(
            vector_index_service.index_single_file,
            str(file_path),
        )
    )
    try:
        return await asyncio.shield(indexing_task)
    except asyncio.CancelledError:
        try:
            await indexing_task
        except Exception:
            logger.exception(f"已取消请求的后台索引任务失败: {file_path}")
        raise


def _normalize_indexing_status(indexing_result: Any) -> dict[str, Any]:
    """Validate indexing results so malformed success responses fail closed."""
    if hasattr(indexing_result, "to_dict"):
        raw_status = indexing_result.to_dict()
    elif isinstance(indexing_result, dict):
        raw_status = dict(indexing_result)
    else:
        raise RuntimeError("索引服务未返回有效结果")

    if not isinstance(raw_status, dict):
        raise RuntimeError("索引服务返回结果格式无效")

    status = str(raw_status.get("status") or "").strip().lower()
    try:
        chunk_count = int(raw_status.get("chunk_count") or 0)
        duration_ms = int(raw_status.get("duration_ms") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("索引服务返回计数字段无效") from exc

    if chunk_count < 0 or duration_ms < 0:
        raise RuntimeError("索引服务返回计数字段无效")
    if status == "success" and chunk_count <= 0:
        raise RuntimeError("索引服务返回成功但未生成可检索分片")
    if status not in {"success", "empty", "failed"}:
        raise RuntimeError(f"索引服务返回未知状态: {status or 'missing'}")

    cleaning = raw_status.get("cleaning")
    normalized = {
        "status": status,
        "chunk_count": chunk_count,
        "duration_ms": duration_ms,
        "error_message": raw_status.get("error_message"),
        "message": raw_status.get("message"),
        "cleaning": cleaning if isinstance(cleaning, dict) else {},
    }
    if status == "failed":
        normalized["error_message"] = PUBLIC_INDEXING_ERROR_MESSAGE
        normalized["message"] = "文件已保存，但索引未完成"
    return normalized


def _record_single_file_quality(indexing_result: Any, *, source_path: str) -> None:
    """Persist quality observability without changing a completed indexing outcome."""
    try:
        indexing_quality_service.record_single_file_result(
            indexing_result,
            operation="upload",
            source_path=source_path,
        )
    except Exception:
        logger.exception(f"记录上传索引质量失败: {source_path}")


def _record_failed_file_quality(*, source_path: str) -> None:
    """Best-effort persistence for a failed upload indexing attempt."""
    try:
        indexing_quality_service.record_failed_file(
            source_path=source_path,
            operation="upload",
            error_message=PUBLIC_INDEXING_ERROR_MESSAGE,
        )
    except Exception:
        logger.exception(f"记录上传索引失败质量报告失败: {source_path}")


def _record_directory_quality(result: Any) -> None:
    """Persist directory quality reports without changing the completed indexing outcome."""
    try:
        indexing_quality_service.record_directory_result(result, operation="directory")
    except Exception:
        logger.exception("记录目录索引质量报告失败")


async def _save_upload_file(
    file: UploadFile,
    file_path: Path,
    *,
    file_extension: str = "",
) -> int:
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

        if file_extension:
            _validate_saved_file_signature(temp_path, file_extension)
        temp_path.replace(file_path)
        return uploaded_size
    except BaseException:
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
    if len(filename.encode("utf-8")) > MAX_SAFE_FILENAME_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"文件名过长（UTF-8 编码最大 {MAX_SAFE_FILENAME_BYTES} 字节）",
        )
    device_stem = filename.split(".", 1)[0].rstrip(" .").upper()
    if device_stem in WINDOWS_RESERVED_FILENAMES:
        raise HTTPException(status_code=400, detail="文件名不能使用系统保留名称")


def _validate_upload_filename(filename: str) -> str:
    """Reject path-like or rewritten upload names instead of silently colliding."""
    if unicodedata.normalize("NFC", filename) != filename:
        raise HTTPException(status_code=400, detail="文件名必须使用 NFC Unicode 规范形式")
    if filename != filename.strip():
        raise HTTPException(status_code=400, detail="文件名不能包含首尾空白")
    if any(char in filename for char in ["\\", "/", ":", "*", "?", '"', "<", ">", "|"]):
        raise HTTPException(status_code=400, detail="文件名包含不允许的字符")
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in filename):
        raise HTTPException(status_code=400, detail="文件名包含不允许的字符")
    _validate_safe_filename(filename)
    return filename


def _validate_upload_mime_type(extension: str, content_type: str | None) -> None:
    """Reject explicit MIME types that contradict the selected extension."""
    normalized = str(content_type or "").split(";", 1)[0].strip().lower()
    if normalized in GENERIC_UPLOAD_MIME_TYPES:
        return
    allowed = EXTENSION_MIME_TYPES.get(extension, set())
    if normalized not in allowed:
        raise HTTPException(status_code=400, detail="文件 MIME 类型与扩展名不匹配")


def _validate_saved_file_signature(path: Path, extension: str) -> None:
    """Validate strong binary signatures before replacing an existing upload."""
    content = path.read_bytes()
    prefix = content[:1024]
    normalized_prefix = prefix.lstrip(b"\xef\xbb\xbf \t\r\n")
    is_pdf = normalized_prefix.startswith(b"%PDF-")
    is_zip = prefix.startswith(b"PK\x03\x04")
    if is_pdf and extension != "pdf":
        raise HTTPException(status_code=400, detail="文件内容与扩展名不匹配")
    if is_zip and extension != "xlsx":
        raise HTTPException(status_code=400, detail="文件内容与扩展名不匹配")

    if extension == "pdf" and not is_pdf:
        raise HTTPException(status_code=400, detail="PDF 文件签名无效")
    if extension == "xlsx":
        try:
            with ZipFile(path) as archive:
                names = set(archive.namelist())
        except BadZipFile as exc:
            raise HTTPException(status_code=400, detail="XLSX 文件签名无效") from exc
        required = {"[Content_Types].xml", "xl/workbook.xml"}
        if not required.issubset(names):
            raise HTTPException(status_code=400, detail="XLSX 文件结构无效")

    if extension in {"txt", "md", "markdown", "html", "htm", "csv"} and b"\x00" in content:
        raise HTTPException(status_code=400, detail="文本文件包含二进制内容")
    if extension in {"txt", "md", "markdown"}:
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="文本文件编码必须为 UTF-8") from exc


def _cleanup_stale_upload_temps(file_path: Path) -> None:
    """Remove abandoned temporary files while holding the matching upload lock."""
    prefix = f".{file_path.name}."
    for temp_path in file_path.parent.iterdir():
        if not temp_path.name.startswith(prefix) or not temp_path.name.endswith(".tmp"):
            continue
        if not temp_path.is_file():
            continue
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(f"清理遗留临时上传文件失败: {temp_path}")
