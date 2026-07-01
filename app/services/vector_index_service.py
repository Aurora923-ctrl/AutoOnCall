"""向量索引服务模块"""

from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import config
from app.services.document_splitter_service import document_splitter_service
from app.services.lexical_index_service import lexical_index_service
from app.services.vector_store_manager import vector_store_manager


class IndexingResult:
    """索引结果类"""

    def __init__(self):
        self.success = False

        self.directory_path = ""

        self.total_files = 0
        self.success_count = 0
        self.fail_count = 0
        self.success_files: list[dict[str, Any]] = []

        self.start_time: datetime | None = None
        self.end_time: datetime | None = None

        self.error_message = ""

        self.failed_files: dict[str, str] = {}

        self.empty_count = 0
        self.empty_files: dict[str, str] = {}

    def increment_success_count(self):
        """增加成功计数"""
        self.success_count += 1

    def increment_fail_count(self):
        """增加失败计数"""
        self.fail_count += 1

    def increment_empty_count(self):
        """增加空索引计数"""
        self.empty_count += 1

    def add_failed_file(self, file_path: str, error: str):
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

    def add_empty_file(self, file_path: str, message: str):
        """添加未产生 chunk 的文件"""
        self.empty_files[file_path] = message

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
            "success_files": self.success_files,
            "failed_files": self.failed_files,
            "empty_count": self.empty_count,
            "empty_files": self.empty_files,
        }


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
    ) -> None:
        self.file_path = file_path
        self.status = status
        self.chunk_count = chunk_count
        self.error_message = error_message
        self.message = message
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
        }


class VectorIndexService:
    """向量索引服务 - 负责读取文件、生成向量、存储到 Milvus"""

    def __init__(self):
        """初始化向量索引服务"""
        self.upload_path = "./uploads"
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
                raise ValueError(f"目录不存在或不是有效目录: {target_path}")

            result.directory_path = str(dir_path)

            files = (
                list(dir_path.glob("*.txt"))
                + list(dir_path.glob("*.md"))
                + list(dir_path.glob("*.markdown"))
            )

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
                    if (
                        isinstance(file_result, SingleFileIndexingResult)
                        and file_result.status == "empty"
                    ):
                        result.increment_empty_count()
                        result.add_empty_file(
                            str(file_path), file_result.message or "文件未产生可检索分片"
                        )
                        logger.warning(f"⚠ 文件未产生可检索分片: {file_path.name}")
                    else:
                        chunk_count = (
                            file_result.chunk_count
                            if isinstance(file_result, SingleFileIndexingResult)
                            else 0
                        )
                        result.increment_success_count()
                        result.add_success_file(
                            str(file_path),
                            chunk_count,
                            (
                                file_result.message
                                if isinstance(file_result, SingleFileIndexingResult)
                                else None
                            ),
                        )
                        logger.info(f"✓ 文件索引成功: {file_path.name}")
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

        except Exception as e:
            logger.error(f"索引目录失败: {e}")
            result.success = False
            result.error_message = str(e)
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

        try:
            content = path.read_text(encoding="utf-8")
            logger.info(f"读取文件: {path}, 内容长度: {len(content)} 字符")

            documents = document_splitter_service.split_document(content, normalized_path)
            logger.info(f"文档分割完成: {file_path} -> {len(documents)} 个分片")

            if documents:
                document_version = str(documents[0].metadata.get("_document_version") or "")
                vector_store_manager.add_documents(documents)
                vector_store_manager.delete_by_source_except_version(
                    normalized_path,
                    document_version,
                )
                lexical_index_service.upsert_source(normalized_path, documents)
                logger.info(f"文件索引完成: {file_path}, 共 {len(documents)} 个分片")
                return SingleFileIndexingResult(
                    file_path=normalized_path,
                    status="success",
                    chunk_count=len(documents),
                    message="文件索引完成",
                ).finish()
            else:
                vector_deleted = vector_store_manager.delete_by_source(normalized_path)
                lexical_deleted = lexical_index_service.delete_source(normalized_path)
                logger.warning(f"文件内容为空或无法分割: {file_path}")
                return SingleFileIndexingResult(
                    file_path=normalized_path,
                    status="empty",
                    chunk_count=0,
                    message=(
                        "文件内容为空或无法切分，未写入向量索引；"
                        f"已清理旧索引 vector={vector_deleted}, lexical={lexical_deleted}"
                    ),
                ).finish()

        except Exception as e:
            logger.error(f"索引文件失败: {file_path}, 错误: {e}")
            try:
                lexical_index_service.mark_source_stale(normalized_path, str(e))
            except Exception as stale_exc:
                logger.warning(f"标记陈旧索引失败: {normalized_path}, 错误: {stale_exc}")
            raise RuntimeError(f"索引文件失败: {e}") from e

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
        raise ValueError(f"{kind}不在允许索引范围内: {path}; allowed_roots={allowed_display}")

    def _allowed_index_roots(self) -> list[Path]:
        raw_roots = [
            item.strip()
            for item in str(config.index_allowed_roots or "").split(",")
            if item.strip()
        ]
        if not raw_roots:
            raw_roots = [self.upload_path]
        return [Path(root).resolve() for root in raw_roots]


vector_index_service = VectorIndexService()
