"""向量存储管理器 - 封装 Milvus VectorStore 操作"""

import hashlib
import time
from contextlib import nullcontext
from threading import RLock
from typing import Any, cast

from langchain_core.documents import Document
from langchain_milvus import Milvus
from loguru import logger

from app.config import config
from app.core.milvus_client import milvus_manager
from app.core.resilience import call_sync_with_resilience
from app.services.document_splitter_service import canonical_source_id
from app.services.vector_embedding_service import vector_embedding_service
from app.utils.log_safety import summarize_text_for_log

COLLECTION_NAME = "biz"
MAX_SIMILARITY_SEARCH_K = 500


class VectorStoreManager:
    """向量存储管理器"""

    def __init__(self) -> None:
        """初始化向量存储管理器"""
        self.vector_store: Milvus | None = None
        self._lock = RLock()

        self.collection_name = COLLECTION_NAME

        logger.info("VectorStore 管理器初始化完成，等待首次使用时连接 Milvus")

    def _initialize_vector_store(self) -> None:
        """初始化 Milvus VectorStore"""
        if self.vector_store is not None:
            return

        try:
            _ = milvus_manager.connect()

            connection_args = {
                "uri": f"http://{config.milvus_host}:{config.milvus_port}",
                "timeout": config.milvus_timeout / 1000,
            }

            self.vector_store = Milvus(
                embedding_function=vector_embedding_service,
                collection_name=self.collection_name,
                connection_args=connection_args,
                auto_id=False,  # 使用自定义 id
                drop_old=False,
                text_field="content",  # 文本内容存储到 content 字段
                vector_field="vector",  # 向量存储到 vector 字段
                primary_field="id",  # 主键字段
                metadata_field="metadata",  # 元数据字段
                timeout=config.milvus_timeout / 1000,
            )

            logger.info(
                f"VectorStore 初始化成功: {config.milvus_host}:{config.milvus_port}, "
                f"collection: {self.collection_name}"
            )

        except Exception as e:
            logger.error(f"VectorStore 初始化失败: {e}")
            raise

    def _ensure_vector_store(self) -> Milvus:
        """Initialize and return the Milvus VectorStore on demand."""
        if self.vector_store is None:
            with self._lock:
                if self.vector_store is None:
                    self._initialize_vector_store()
        if self.vector_store is None:
            raise RuntimeError("VectorStore 初始化失败")
        return self.vector_store

    def add_documents(self, documents: list[Document]) -> list[str]:
        """
        批量添加文档到向量存储（自动批量向量化）

        Args:
            documents: 文档列表

        Returns:
            List[str]: 文档 ID 列表
        """
        try:
            if not documents:
                logger.info("没有待写入的文档，跳过 VectorStore 写入")
                return []

            start_time = time.time()

            vector_store = self._ensure_vector_store()

            ids = [
                build_vector_document_id(document, index)
                for index, document in enumerate(documents, 1)
            ]
            for document, document_id in zip(documents, ids, strict=True):
                document.metadata["_vector_id"] = document_id

            if not hasattr(vector_store, "upsert"):
                raise RuntimeError(
                    f"Milvus collection '{self.collection_name}' 未就绪，拒绝降级为 insert"
                )
            try:

                def upsert() -> None:
                    cast(Any, vector_store).upsert(
                        ids=ids,
                        documents=documents,
                        batch_size=len(documents),
                        timeout=config.milvus_timeout / 1000,
                    )
                    self._flush(vector_store)

                call_sync_with_resilience(
                    "milvus",
                    "upsert",
                    upsert,
                    timeout_seconds=config.milvus_timeout / 1000,
                    max_attempts=1,
                    retry_delay_seconds=0,
                    is_retryable=_is_retryable_milvus_error,
                    failure_threshold=config.dependency_circuit_failure_threshold,
                    recovery_timeout_seconds=config.dependency_circuit_recovery_seconds,
                )
            except Exception:
                self._delete_vector_ids_with_store(
                    vector_store,
                    ids,
                    raise_on_error=False,
                )
                raise
            result_ids = ids

            elapsed = time.time() - start_time
            logger.info(
                f"批量添加 {len(documents)} 个文档到 VectorStore 完成, "
                f"耗时: {elapsed:.2f}秒, 平均: {elapsed / len(documents):.2f}秒/个"
            )
            return result_ids
        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            raise

    def _collection_exists(self, vector_store: Milvus) -> bool:
        """Return whether the backing collection already exists."""
        client = getattr(vector_store, "client", None)
        if client is None or not hasattr(client, "has_collection"):
            return False
        collection_name = str(getattr(vector_store, "collection_name", self.collection_name))
        try:
            return bool(client.has_collection(collection_name))
        except Exception as exc:
            raise RuntimeError(f"检查 Milvus collection 是否存在失败: {exc}") from exc

    def _flush(self, vector_store: Milvus) -> None:
        """Wait for a completed write to become visible before cleanup starts."""
        client = getattr(vector_store, "client", None)
        if client is None or not hasattr(client, "flush"):
            raise RuntimeError("Milvus client 不支持 flush，无法确认写入可见性")
        client.flush(
            collection_name=self.collection_name,
            timeout=config.milvus_timeout / 1000,
        )

    def _delete_vector_ids_with_store(
        self,
        vector_store: Milvus,
        vector_ids: list[str],
        *,
        raise_on_error: bool,
    ) -> int:
        """Delete a known batch using the same Milvus client that performed the write."""
        unique_ids = sorted({str(item) for item in vector_ids if str(item)})
        if not unique_ids:
            return 0
        client = getattr(vector_store, "client", None)
        if client is None or not hasattr(client, "delete"):
            if raise_on_error:
                raise RuntimeError("Milvus client 不支持 delete，无法补偿部分写入")
            return 0
        try:
            result = client.delete(
                collection_name=self.collection_name,
                ids=unique_ids,
                timeout=config.milvus_timeout / 1000,
            )
            self._flush(vector_store)
            deleted_count = (
                result.delete_count if hasattr(result, "delete_count") else len(unique_ids)
            )
            logger.info(
                f"同客户端补偿删除向量批次完成: ids={len(unique_ids)}, deleted={deleted_count}"
            )
            return deleted_count
        except Exception as exc:
            logger.error(f"同客户端补偿删除向量批次失败: {exc}")
            if raise_on_error:
                raise
            return 0

    def delete_by_source(self, file_path: str, *, raise_on_error: bool = False) -> int:
        """
        删除指定文件的所有文档

        Args:
            file_path: 文件路径

        Returns:
            int: 删除的文档数量
        """
        try:
            with self._collection_session() as collection:
                expr = _source_match_expr(file_path)
                result = collection.delete(expr, timeout=config.milvus_timeout / 1000)
                collection.flush(timeout=config.milvus_timeout / 1000)
            deleted_count = result.delete_count if hasattr(result, "delete_count") else 0

            logger.info(f"删除文件旧数据: {file_path}, 删除数量: {deleted_count}")
            return deleted_count

        except Exception as e:
            logger.warning(f"删除旧数据失败 (可能是首次索引): {e}")
            if raise_on_error:
                raise
            return 0

    def delete_by_source_except_version(
        self,
        file_path: str,
        document_version: str,
        *,
        raise_on_error: bool = False,
    ) -> int:
        """Delete older chunks for one source while preserving the newly indexed version."""
        if not document_version:
            return self.delete_by_source(file_path, raise_on_error=raise_on_error)
        try:
            with self._collection_session() as collection:
                expr = (
                    f"({_source_match_expr(file_path)}) "
                    f'and metadata["_document_version"] != {_quote_expr_value(document_version)}'
                )
                result = collection.delete(expr, timeout=config.milvus_timeout / 1000)
                collection.flush(timeout=config.milvus_timeout / 1000)
            deleted_count = result.delete_count if hasattr(result, "delete_count") else 0

            logger.info(
                f"删除文件旧版本数据: {file_path}, version={document_version}, "
                f"删除数量: {deleted_count}"
            )
            return deleted_count

        except Exception as e:
            logger.warning(f"删除旧版本数据失败，可能短期存在重复 chunk: {e}")
            if raise_on_error:
                raise
            return 0

    def delete_by_source_except_ids(
        self,
        file_path: str,
        vector_ids: list[str],
        *,
        raise_on_error: bool = True,
    ) -> int:
        """Delete chunks for one source that are not part of the latest indexed batch."""
        unique_ids = sorted({str(item) for item in vector_ids if str(item)})
        if not unique_ids:
            return self.delete_by_source(file_path, raise_on_error=raise_on_error)
        try:
            with self._collection_session() as collection:
                id_list = ", ".join(_quote_expr_value(vector_id) for vector_id in unique_ids)
                expr = f"({_source_match_expr(file_path)}) and id not in [{id_list}]"
                result = collection.delete(expr, timeout=config.milvus_timeout / 1000)
                collection.flush(timeout=config.milvus_timeout / 1000)
            deleted_count = result.delete_count if hasattr(result, "delete_count") else 0

            logger.info(
                f"删除文件非当前批次数据: {file_path}, 保留数量={len(unique_ids)}, "
                f"删除数量: {deleted_count}"
            )
            return deleted_count

        except Exception as e:
            logger.error(f"删除非当前批次数据失败: {e}")
            if raise_on_error:
                raise RuntimeError(f"删除非当前批次向量数据失败: {e}") from e
            return 0

    def delete_by_ids(self, vector_ids: list[str], *, raise_on_error: bool = False) -> int:
        """Delete a known batch of vector IDs for failed cross-index compensation."""
        unique_ids = sorted({str(item) for item in vector_ids if str(item)})
        if not unique_ids:
            return 0
        try:
            with self._collection_session() as collection:
                id_list = ", ".join(_quote_expr_value(vector_id) for vector_id in unique_ids)
                result = collection.delete(
                    f"id in [{id_list}]",
                    timeout=config.milvus_timeout / 1000,
                )
                collection.flush(timeout=config.milvus_timeout / 1000)
            deleted_count = result.delete_count if hasattr(result, "delete_count") else 0
            logger.info(f"补偿删除向量批次完成: ids={len(unique_ids)}, deleted={deleted_count}")
            return deleted_count
        except Exception as exc:
            logger.error(f"补偿删除向量批次失败: {exc}")
            if raise_on_error:
                raise
            return 0

    def get_vector_store(self) -> Milvus:
        """
        获取 VectorStore 实例

        Returns:
            Milvus: VectorStore 实例
        """
        return self._ensure_vector_store()

    def similarity_search(
        self,
        query: str,
        k: int = 3,
        *,
        expr: str | None = None,
    ) -> list[Document]:
        """
        相似度搜索

        Args:
            query: 查询文本
            k: 返回结果数量

        Returns:
            List[Document]: 相关文档列表
        """
        safe_query, safe_expr = _validate_search_inputs(query, k=k, expr=expr)
        try:
            vector_store = cast(Any, self._ensure_vector_store())

            def search() -> Any:
                return (
                    vector_store.similarity_search(safe_query, k=k, expr=safe_expr)
                    if safe_expr
                    else vector_store.similarity_search(safe_query, k=k)
                )

            docs = call_sync_with_resilience(
                "milvus",
                "similarity_search",
                search,
                timeout_seconds=config.milvus_timeout / 1000,
                max_attempts=int(config.milvus_connect_max_retries) + 1,
                retry_delay_seconds=float(config.milvus_connect_retry_delay_seconds),
                is_retryable=_is_retryable_milvus_error,
                failure_threshold=config.dependency_circuit_failure_threshold,
                recovery_timeout_seconds=config.dependency_circuit_recovery_seconds,
            )
            logger.debug(
                "相似度搜索完成: {}, 结果数={}",
                summarize_text_for_log(safe_query, label="query"),
                len(docs),
            )
            return cast(list[Document], docs)
        except Exception as e:
            logger.error(
                "相似度搜索失败: {}, error_type={}",
                summarize_text_for_log(safe_query, label="query"),
                type(e).__name__,
            )
            raise

    def similarity_search_with_score(
        self,
        query: str,
        k: int = 3,
        *,
        expr: str | None = None,
    ) -> list[tuple[Document, float]]:
        """Run scored similarity search through the same validated manager boundary."""
        safe_query, safe_expr = _validate_search_inputs(query, k=k, expr=expr)
        try:
            vector_store = cast(Any, self._ensure_vector_store())
            if not hasattr(vector_store, "similarity_search_with_score"):
                raise RuntimeError("VectorStore 不支持带分数的相似度检索")

            def scored_search() -> Any:
                return (
                    vector_store.similarity_search_with_score(safe_query, k=k, expr=safe_expr)
                    if safe_expr
                    else vector_store.similarity_search_with_score(safe_query, k=k)
                )

            results = call_sync_with_resilience(
                "milvus",
                "similarity_search_with_score",
                scored_search,
                timeout_seconds=config.milvus_timeout / 1000,
                max_attempts=int(config.milvus_connect_max_retries) + 1,
                retry_delay_seconds=float(config.milvus_connect_retry_delay_seconds),
                is_retryable=_is_retryable_milvus_error,
                failure_threshold=config.dependency_circuit_failure_threshold,
                recovery_timeout_seconds=config.dependency_circuit_recovery_seconds,
            )
            logger.debug(
                "带分数相似度搜索完成: {}, 结果数: {}",
                summarize_text_for_log(safe_query, label="query"),
                len(results),
            )
            return cast(list[tuple[Document, float]], results)
        except Exception as exc:
            logger.error(
                "带分数相似度搜索失败: {}, error_type={}",
                summarize_text_for_log(safe_query, label="query"),
                type(exc).__name__,
            )
            raise

    def close(self) -> None:
        """Close clients owned by the LangChain vector store."""
        vector_store = self._detach_vector_store()
        if vector_store is None:
            return
        client = getattr(vector_store, "client", None)
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception as exc:
                logger.warning(f"关闭 VectorStore MilvusClient 失败: {exc}")

    async def aclose(self) -> None:
        """Close both synchronous and asynchronous clients owned by the vector store."""
        vector_store = self._detach_vector_store()
        if vector_store is None:
            return
        async_client = getattr(vector_store, "_async_milvus_client", None)
        if async_client is not None and hasattr(async_client, "close"):
            try:
                await async_client.close()
            except Exception as exc:
                logger.warning(f"关闭 VectorStore AsyncMilvusClient 失败: {exc}")
        client = getattr(vector_store, "client", None)
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception as exc:
                logger.warning(f"关闭 VectorStore MilvusClient 失败: {exc}")

    def _detach_vector_store(self) -> Milvus | None:
        with self._lock:
            vector_store = self.vector_store
            self.vector_store = None
        return vector_store

    def _collection_session(self) -> Any:
        """Use the manager lifecycle lock when available, while preserving test doubles."""
        session = getattr(milvus_manager, "collection_session", None)
        if callable(session):
            return session()
        _ = milvus_manager.connect()
        return nullcontext(milvus_manager.get_collection())


vector_store_manager = VectorStoreManager()


def _is_retryable_milvus_error(exc: Exception) -> bool:
    """Retry transient Milvus transport and timeout failures only."""

    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    error_name = type(exc).__name__.lower()
    message = str(exc).lower()
    return any(
        marker in error_name or marker in message
        for marker in (
            "timeout",
            "timed out",
            "connection",
            "unavailable",
            "resource exhausted",
            "rate limit",
        )
    )


def _quote_expr_value(value: Any) -> str:
    """Quote one value for a Milvus metadata expression."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _validate_search_inputs(
    query: Any,
    *,
    k: Any,
    expr: Any,
) -> tuple[str, str | None]:
    safe_query = str(query or "").strip()
    if not safe_query:
        raise ValueError("query 不能为空")
    if len(safe_query) > 8000:
        raise ValueError("query 长度不能超过 8000")
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("k 必须是正整数")
    if k > MAX_SIMILARITY_SEARCH_K:
        raise ValueError(f"k 不能超过 {MAX_SIMILARITY_SEARCH_K}")
    if expr is not None and not isinstance(expr, str):
        raise TypeError("expr 必须是字符串或 None")
    return safe_query, expr.strip() if expr else None


def _source_match_expr(file_path: str) -> str:
    """Match a source by canonical identity with an exact-path fallback for legacy rows."""
    source_path = str(file_path or "")
    source_id = canonical_source_id(source_path)
    return (
        f'metadata["_source_id"] == {_quote_expr_value(source_id)} '
        f'or metadata["_source"] == {_quote_expr_value(source_path)}'
    )


def build_vector_document_id(document: Document, index: int = 1) -> str:
    """Build a stable Milvus primary key for one document chunk."""
    metadata = dict(document.metadata or {})
    identity_parts = [
        _canonical_source_id(metadata),
        str(metadata.get("_chunk_id") or index),
        str(metadata.get("_chunk_hash") or ""),
    ]
    identity = "\x1f".join(identity_parts)
    return f"vec-{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _canonical_source_id(metadata: dict[str, Any]) -> str:
    explicit = str(metadata.get("_source_id") or "").strip()
    if explicit:
        return explicit
    source = str(metadata.get("_source") or metadata.get("source") or metadata.get("_doc_id") or "")
    return canonical_source_id(source)
