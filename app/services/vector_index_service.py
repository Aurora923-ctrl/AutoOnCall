"""向量索引服务模块"""

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from filelock import FileLock
from loguru import logger

from app.config import config
from app.services.document_loaders import document_loader_registry
from app.services.document_splitter_service import canonical_source_id, document_splitter_service
from app.services.lexical_index_service import lexical_index_service
from app.services.vector_store_manager import vector_store_manager


class IndexingResult:
    """索引结果类"""

    def __init__(self) -> None:
        self.success = False

        self.directory_path = ""

        self.total_files = 0
        self.success_count = 0
        self.fail_count = 0
        self.success_files: list[dict[str, Any]] = []

        self.start_time: datetime | None = None
        self.end_time: datetime | None = None

        self.error_message = ""
        self.error_type = ""

        self.failed_files: dict[str, str] = {}

        self.empty_count = 0
        self.empty_files: dict[str, str] = {}
        self.cleaning_reports: dict[str, dict[str, Any]] = {}

    def increment_success_count(self) -> None:
        """增加成功计数"""
        self.success_count += 1

    def increment_fail_count(self) -> None:
        """增加失败计数"""
        self.fail_count += 1

    def increment_empty_count(self) -> None:
        """增加空索引计数"""
        self.empty_count += 1

    def add_failed_file(self, file_path: str, error: str) -> None:
        """添加失败文件"""
        self.failed_files[file_path] = error

    def add_success_file(
        self, file_path: str, chunk_count: int, message: str | None = None
    ) -> None:
        """Add one successfully indexed file summary."""
        self.success_files.append(
            {
                "file_path": file_path,
                "chunk_count": chunk_count,
                "message": message or "文件索引完成",
            }
        )

    def add_empty_file(self, file_path: str, message: str) -> None:
        """添加未产生 chunk 的文件"""
        self.empty_files[file_path] = message

    def add_cleaning_report(self, file_path: str, report: dict[str, Any]) -> None:
        """Attach loader-level cleaning details for one indexed file."""
        self.cleaning_reports[file_path] = report

    def get_duration_ms(self) -> int:
        """获取耗时（毫秒）"""
        if self.start_time and self.end_time:
            return int((self.end_time - self.start_time).total_seconds() * 1000)
        return 0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "directory_path": self.directory_path,
            "total_files": self.total_files,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "duration_ms": self.get_duration_ms(),
            "error_message": self.error_message,
            "error_type": self.error_type,
            "success_files": self.success_files,
            "failed_files": self.failed_files,
            "empty_count": self.empty_count,
            "empty_files": self.empty_files,
            "cleaning_reports": self.cleaning_reports,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Return an API-safe result without local filesystem details."""
        payload = self.to_dict()
        payload["directory_path"] = _public_path_label(self.directory_path)
        payload["directory_name"] = _public_path_label(self.directory_path)
        payload["error_message"] = _public_index_error_message(
            self.error_type,
            self.error_message,
        )
        payload["success_files"] = [
            {
                **file_info,
                "file_path": _public_path_label(str(file_info.get("file_path") or "")),
                "file_name": _public_path_label(str(file_info.get("file_path") or "")),
            }
            for file_info in self.success_files
        ]
        payload["failed_files"] = {
            _public_path_label(file_path): _public_index_error_message("file_indexing_error", error)
            for file_path, error in self.failed_files.items()
        }
        payload["empty_files"] = {
            _public_path_label(file_path): message
            for file_path, message in self.empty_files.items()
        }
        payload["cleaning_reports"] = {
            _public_path_label(file_path): report
            for file_path, report in self.cleaning_reports.items()
        }
        return payload


class SingleFileIndexingResult:
    """Result for indexing one uploaded knowledge file."""

    def __init__(
        self,
        *,
        file_path: str,
        status: str,
        chunk_count: int,
        error_message: str | None = None,
        message: str | None = None,
        cleaning_report: dict[str, Any] | None = None,
    ) -> None:
        self.file_path = file_path
        self.status = status
        self.chunk_count = chunk_count
        self.error_message = error_message
        self.message = message
        self.cleaning_report = cleaning_report or {}
        self.start_time = datetime.now()
        self.end_time: datetime | None = None

    def finish(self) -> "SingleFileIndexingResult":
        self.end_time = datetime.now()
        return self

    def get_duration_ms(self) -> int:
        end_time = self.end_time or datetime.now()
        return int((end_time - self.start_time).total_seconds() * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "chunk_count": self.chunk_count,
            "duration_ms": self.get_duration_ms(),
            "error_message": self.error_message,
            "message": self.message,
            "cleaning": self.cleaning_report,
        }


class IndexingValidationError(ValueError):
    """Base error for invalid index targets."""


class IndexPathForbiddenError(IndexingValidationError):
    """Raised when an index target is outside configured allowed roots."""


class InvalidIndexDirectoryError(IndexingValidationError):
    """Raised when a directory target cannot be indexed."""


def _public_path_label(path: str) -> str:
    """Return a display label for a path without exposing parent directories."""
    normalized = str(path or "").replace("\\", "/").rstrip("/")
    if not normalized:
        return ""
    return normalized.rsplit("/", 1)[-1] or "root"


def _public_index_error_message(error_type: str, message: str) -> str:
    """Return an indexing error message safe for API responses."""
    if not message:
        return ""
    if error_type in {"forbidden_directory", "invalid_directory"}:
        return message
    return "索引失败，请检查服务端日志"


class VectorIndexService:
    """向量索引服务 - 负责读取文件、生成向量、存储到 Milvus"""

    def __init__(self) -> None:
        """初始化向量索引服务"""
        self.upload_path = config.upload_dir
        self._source_lock_dir = Path(config.rag_lexical_index_path).parent / ".index-locks"
        logger.info("向量索引服务初始化完成")

    def index_directory(self, directory_path: str | None = None) -> IndexingResult:
        """
        索引指定目录下的所有文件

        Args:
            directory_path: 目录路径（可选，默认使用配置的上传目录）

        Returns:
            IndexingResult: 索引结果
        """
        result = IndexingResult()
        result.start_time = datetime.now()

        try:
            target_path = directory_path if directory_path else self.upload_path

            dir_path = Path(target_path).resolve()
            self._ensure_directory_allowed(dir_path)

            if not dir_path.exists() or not dir_path.is_dir():
                raise InvalidIndexDirectoryError("目录不存在或不是有效目录")

            result.directory_path = str(dir_path)

            files = self._supported_files(dir_path)

            if not files:
                logger.warning(f"目录中没有找到支持的文件: {target_path}")
                result.total_files = 0
                result.success = True
                result.end_time = datetime.now()
                return result

            result.total_files = len(files)
            logger.info(f"开始索引目录: {target_path}, 找到 {len(files)} 个文件")

            for file_path in files:
                try:
                    file_result = self.index_single_file(str(file_path))
                    if not isinstance(file_result, SingleFileIndexingResult):
                        raise RuntimeError("单文件索引服务未返回有效结果")
                    if file_result.status == "empty":
                        result.increment_empty_count()
                        result.add_empty_file(
                            str(file_path), file_result.message or "文件未产生可检索分片"
                        )
                        result.add_cleaning_report(
                            str(file_path),
                            file_result.cleaning_report,
                        )
                        logger.warning(f"⚠ 文件未产生可检索分片: {file_path.name}")
                    elif file_result.status == "success" and file_result.chunk_count > 0:
                        result.increment_success_count()
                        result.add_success_file(
                            str(file_path),
                            file_result.chunk_count,
                            file_result.message,
                        )
                        result.add_cleaning_report(
                            str(file_path),
                            file_result.cleaning_report,
                        )
                        logger.info(f"✓ 文件索引成功: {file_path.name}")
                    else:
                        raise RuntimeError(
                            "单文件索引结果无效: "
                            f"status={file_result.status}, chunk_count={file_result.chunk_count}"
                        )
                except Exception as e:
                    result.increment_fail_count()
                    result.add_failed_file(str(file_path), str(e))
                    logger.error(f"✗ 文件索引失败: {file_path.name}, 错误: {e}")

            result.success = result.fail_count == 0 and result.empty_count == 0
            result.end_time = datetime.now()

            logger.info(
                f"目录索引完成: 总数={result.total_files}, "
                f"成功={result.success_count}, 失败={result.fail_count}"
            )

            return result

        except IndexPathForbiddenError as e:
            logger.error(f"索引目录被拒绝: {e}")
            result.success = False
            result.error_message = str(e)
            result.error_type = "forbidden_directory"
            result.end_time = datetime.now()
            return result
        except InvalidIndexDirectoryError as e:
            logger.error(f"索引目录参数无效: {e}")
            result.success = False
            result.error_message = str(e)
            result.error_type = "invalid_directory"
            result.end_time = datetime.now()
            return result
        except Exception as e:
            logger.error(f"索引目录失败: {e}")
            result.success = False
            result.error_message = str(e)
            result.error_type = "indexing_error"
            result.end_time = datetime.now()
            return result

    def index_single_file(self, file_path: str) -> SingleFileIndexingResult:
        """
        索引单个文件 (使用新的 LangChain 分割器)

        Args:
            file_path: 文件路径

        Raises:
            ValueError: 文件不存在时抛出
            RuntimeError: 索引失败时抛出
        """
        path = Path(file_path).resolve()

        if not path.exists() or not path.is_file():
            raise ValueError(f"文件不存在: {file_path}")
        self._ensure_file_allowed(path)
        if path.stat().st_size > config.upload_max_file_size:
            raise ValueError(
                f"文件大小超过索引限制（最大 {config.upload_max_file_size_mb}MB）: {file_path}"
            )

        logger.info(f"开始索引文件: {path}")
        normalized_path = path.as_posix()
        rollback_succeeded = False

        try:
            with self._source_lock(normalized_path):
                lexical_snapshot = self._snapshot_lexical_source(normalized_path)
                try:
                    return self._index_single_file_locked(
                        path,
                        normalized_path,
                        lexical_snapshot,
                    )
                except Exception:
                    rollback_succeeded = self._restore_lexical_source(
                        normalized_path,
                        lexical_snapshot,
                    )
                    raise

        except Exception as e:
            logger.error(f"索引文件失败: {file_path}, 错误: {e}")
            if not rollback_succeeded:
                try:
                    lexical_index_service.mark_source_stale(normalized_path, str(e))
                except Exception as stale_exc:
                    logger.warning(f"标记陈旧索引失败: {normalized_path}, 错误: {stale_exc}")
            raise RuntimeError(f"索引文件失败: {e}") from e

    def _index_single_file_locked(
        self,
        path: Path,
        normalized_path: str,
        lexical_snapshot: dict[str, Any],
    ) -> SingleFileIndexingResult:
        """Index one source while holding its cross-process transaction lock."""
        self._mark_source_stale(normalized_path, "indexing_in_progress")
        loader = document_loader_registry.get_loader(path)
        loaded_documents, cleaning_report = loader.load(path)
        logger.info(
            f"读取文件: {path}, loader={loader.loader_type}, "
            f"有效单元={len(loaded_documents)}, raw_units={cleaning_report.raw_units}"
        )

        documents = document_splitter_service.split_loaded_documents(
            loaded_documents,
            normalized_path,
        )
        self._preserve_existing_chunk_ids(documents, lexical_snapshot)
        logger.info(f"文档分割完成: {path} -> {len(documents)} 个分片")

        if documents:
            vector_ids: list[str] = []
            try:
                vector_ids = vector_store_manager.add_documents(documents)
                self._replace_lexical_source_keep_stale(normalized_path, documents)
                vector_store_manager.delete_by_source_except_ids(
                    normalized_path,
                    vector_ids,
                    raise_on_error=True,
                )
                lexical_index_service.clear_source_stale(normalized_path)
            except Exception:
                if vector_ids:
                    compensator = getattr(vector_store_manager, "delete_by_ids", None)
                    if callable(compensator):
                        compensator(vector_ids, raise_on_error=False)
                raise
            logger.info(f"文件索引完成: {path}, 共 {len(documents)} 个分片")
            return SingleFileIndexingResult(
                file_path=normalized_path,
                status="success",
                chunk_count=len(documents),
                message="文件索引完成",
                cleaning_report=cleaning_report.model_dump(mode="json"),
            ).finish()

        vector_deleted = vector_store_manager.delete_by_source(
            normalized_path,
            raise_on_error=True,
        )
        lexical_deleted = lexical_index_service.delete_source(normalized_path)
        logger.warning(f"文件内容为空或无法分割: {path}")
        return SingleFileIndexingResult(
            file_path=normalized_path,
            status="empty",
            chunk_count=0,
            message=(
                "文件内容为空或无法切分，未写入向量索引；"
                f"已清理旧索引 vector={vector_deleted}, lexical={lexical_deleted}"
            ),
            cleaning_report=cleaning_report.model_dump(mode="json"),
        ).finish()

    def _source_lock(self, source_path: str) -> FileLock:
        """Serialize updates for one source across threads and worker processes."""
        self._source_lock_dir.mkdir(parents=True, exist_ok=True)
        lock_id = hashlib.sha256(canonical_source_id(source_path).encode("utf-8")).hexdigest()
        return FileLock(str(self._source_lock_dir / f"{lock_id}.lock"))

    @staticmethod
    def _snapshot_lexical_source(source_path: str) -> dict[str, Any]:
        snapshot = getattr(lexical_index_service, "snapshot_source_state", None)
        return snapshot(source_path) if callable(snapshot) else {"chunks": [], "stale_reason": None}

    @staticmethod
    def _restore_lexical_source(source_path: str, snapshot: dict[str, Any]) -> bool:
        restore = getattr(lexical_index_service, "restore_source_state", None)
        if callable(restore):
            restore(source_path, snapshot)
            return True
        return False

    @staticmethod
    def _preserve_existing_chunk_ids(
        documents: list[Any],
        lexical_snapshot: dict[str, Any],
    ) -> None:
        """Keep unchanged chunk citations stable across local document edits."""
        existing_by_hash: dict[str, list[str]] = {}
        for chunk in lexical_snapshot.get("chunks") or []:
            metadata = dict(chunk.get("metadata") or {})
            chunk_hash = str(metadata.get("_chunk_hash") or "")
            chunk_id = str(metadata.get("_chunk_id") or chunk.get("chunk_id") or "")
            if chunk_hash and chunk_id:
                existing_by_hash.setdefault(chunk_hash, []).append(chunk_id)

        used_ids: set[str] = set()
        for document in documents:
            metadata = dict(document.metadata or {})
            chunk_hash = str(metadata.get("_chunk_hash") or "")
            matches = existing_by_hash.get(chunk_hash) or []
            preserved = next((chunk_id for chunk_id in matches if chunk_id not in used_ids), "")
            if preserved:
                metadata["_chunk_id"] = preserved
            elif lexical_snapshot.get("chunks"):
                file_name = str(metadata.get("_file_name") or "document")
                metadata["_chunk_id"] = f"{file_name}#h-{chunk_hash[:12]}"
            used_ids.add(str(metadata.get("_chunk_id") or ""))
            document.metadata = metadata

    @staticmethod
    def _mark_source_stale(source_path: str, reason: str) -> None:
        marker = getattr(lexical_index_service, "mark_source_stale", None)
        if callable(marker):
            marker(source_path, reason)

    @staticmethod
    def _replace_lexical_source_keep_stale(
        source_path: str,
        documents: list[Any],
    ) -> None:
        lexical_index_service.upsert_source(source_path, documents, clear_stale=False)

    def _ensure_directory_allowed(self, dir_path: Path) -> None:
        """Ensure batch indexing cannot read arbitrary local directories."""
        self._ensure_path_allowed(dir_path, kind="目录")

    def _ensure_file_allowed(self, file_path: Path) -> None:
        """Ensure direct file indexing cannot read arbitrary local files."""
        self._ensure_path_allowed(file_path, kind="文件")

    def _ensure_path_allowed(self, path: Path, *, kind: str) -> None:
        """Ensure indexing is scoped to configured allowed roots."""
        allowed_roots = self._allowed_index_roots()
        for root in allowed_roots:
            try:
                path.relative_to(root)
                return
            except ValueError:
                continue
        allowed_display = ", ".join(str(root) for root in allowed_roots)
        logger.warning(f"{kind}不在允许索引范围内: {path}; allowed_roots={allowed_display}")
        raise IndexPathForbiddenError(f"{kind}不在允许索引范围内")

    def _allowed_index_roots(self) -> list[Path]:
        raw_roots = [
            item.strip()
            for item in str(config.index_allowed_roots or "").split(",")
            if item.strip()
        ]
        if not raw_roots:
            raw_roots = [self.upload_path]
        elif self.upload_path:
            raw_roots.append(self.upload_path)

        resolved_roots = []
        seen: set[Path] = set()
        for root in raw_roots:
            resolved = Path(root).resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            resolved_roots.append(resolved)
        return resolved_roots

    def _supported_files(self, dir_path: Path) -> list[Path]:
        """Return files supported by the configured loader registry."""
        supported = document_loader_registry.supported_extensions
        return sorted(
            path
            for path in dir_path.iterdir()
            if path.is_file() and path.suffix.lower().removeprefix(".") in supported
        )


vector_index_service = VectorIndexService()
