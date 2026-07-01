"""向量存储管理器 - 封装 Milvus VectorStore 操作"""

from typing import Any, cast

from langchain_core.documents import Document
from langchain_milvus import Milvus
from loguru import logger

from app.config import config
from app.core.milvus_client import milvus_manager
from app.services.vector_embedding_service import vector_embedding_service

COLLECTION_NAME = "biz"


class VectorStoreManager:
    """向量存储管理器"""

    def __init__(self):
        """初始化向量存储管理器"""
        self.vector_store: Milvus | None = None

        self.collection_name = COLLECTION_NAME

        logger.info("VectorStore 管理器初始化完成，等待首次使用时连接 Milvus")

    def _initialize_vector_store(self):
        """初始化 Milvus VectorStore"""
        if self.vector_store is not None:
            return

        try:
            _ = milvus_manager.connect()

            connection_args = {
                "host": config.milvus_host,
                "port": config.milvus_port,
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

            import time
            import uuid

            start_time = time.time()

            vector_store = self._ensure_vector_store()

            ids = [str(uuid.uuid4()) for _ in documents]

            result_ids = vector_store.add_documents(documents, ids=ids)

            elapsed = time.time() - start_time
            logger.info(
                f"批量添加 {len(documents)} 个文档到 VectorStore 完成, "
                f"耗时: {elapsed:.2f}秒, 平均: {elapsed/len(documents):.2f}秒/个"
            )
            return result_ids
        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            raise

    def delete_by_source(self, file_path: str) -> int:
        """
        删除指定文件的所有文档

        Args:
            file_path: 文件路径

        Returns:
            int: 删除的文档数量
        """
        try:
            _ = milvus_manager.connect()
            collection = milvus_manager.get_collection()

            expr = f'metadata["_source"] == {_quote_expr_value(file_path)}'

            result = collection.delete(expr)
            deleted_count = result.delete_count if hasattr(result, "delete_count") else 0

            logger.info(f"删除文件旧数据: {file_path}, 删除数量: {deleted_count}")
            return deleted_count

        except Exception as e:
            logger.warning(f"删除旧数据失败 (可能是首次索引): {e}")
            return 0

    def delete_by_source_except_version(self, file_path: str, document_version: str) -> int:
        """Delete older chunks for one source while preserving the newly indexed version."""
        if not document_version:
            return self.delete_by_source(file_path)
        try:
            _ = milvus_manager.connect()
            collection = milvus_manager.get_collection()

            expr = (
                f'metadata["_source"] == {_quote_expr_value(file_path)} '
                f'and metadata["_document_version"] != {_quote_expr_value(document_version)}'
            )

            result = collection.delete(expr)
            deleted_count = result.delete_count if hasattr(result, "delete_count") else 0

            logger.info(
                f"删除文件旧版本数据: {file_path}, version={document_version}, "
                f"删除数量: {deleted_count}"
            )
            return deleted_count

        except Exception as e:
            logger.warning(f"删除旧版本数据失败，可能短期存在重复 chunk: {e}")
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
        try:
            vector_store = cast(Any, self._ensure_vector_store())
            docs = (
                vector_store.similarity_search(query, k=k, expr=expr)
                if expr
                else vector_store.similarity_search(query, k=k)
            )
            logger.debug(f"相似度搜索完成: query='{query}', 结果数={len(docs)}")
            return cast(list[Document], docs)
        except Exception as e:
            logger.error(f"相似度搜索失败: {e}")
            return []


vector_store_manager = VectorStoreManager()


def _quote_expr_value(value: Any) -> str:
    """Quote one value for a Milvus metadata expression."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
