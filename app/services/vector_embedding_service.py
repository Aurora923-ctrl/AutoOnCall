"""向量嵌入服务模块 - 基于 LangChain Embeddings 标准接口"""

import math
import time
from collections.abc import Iterable
from threading import RLock
from typing import SupportsFloat, SupportsIndex, cast

from langchain_core.embeddings import Embeddings
from loguru import logger
from openai import OpenAI

from app.config import config


class DashScopeEmbeddings(Embeddings):
    """阿里云 DashScope Text Embedding (OpenAI 兼容模式)

    实现 LangChain 标准 Embeddings 接口:
    - embed_documents(texts: List[str]) → List[List[float]]: 批量嵌入文档
    - embed_query(text: str) → List[float]: 嵌入单个查询
    """

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-v4",
        dimensions: int = 1024,
        base_url: str | None = None,
    ) -> None:
        """
        初始化 DashScope Embeddings

        Args:
            api_key: DashScope API Key
            model: 嵌入模型名称
            dimensions: 向量维度
        """
        if not api_key or api_key == "your-api-key-here":
            raise ValueError("请设置环境变量 DASHSCOPE_API_KEY")

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url or config.dashscope_api_base,
            timeout=config.dashscope_embedding_timeout_seconds,
            max_retries=0,
        )

        self.model = model
        self.dimensions = dimensions
        self.batch_size = int(config.dashscope_embedding_batch_size)
        self.max_retries = max(0, int(config.dashscope_embedding_max_retries))

        masked_key = self._mask_api_key(api_key)
        logger.info(
            f"DashScope Embeddings 初始化完成 - "
            f"模型: {model}, 维度: {dimensions}, API Key: {masked_key}"
        )

    @staticmethod
    def _mask_api_key(api_key: str) -> str:
        """Return a presence marker without exposing any credential characters."""
        return "configured" if api_key else "missing"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """
        批量嵌入文档列表 (LangChain 标准接口)

        Args:
            texts: 文本列表

        Returns:
            List[List[float]]: 嵌入向量列表
        """
        if not texts:
            return []

        try:
            logger.info(
                f"批量嵌入 {len(texts)} 个文档, batch_size={self.batch_size}, "
                f"max_retries={self.max_retries}"
            )

            embeddings: list[list[float]] = []
            for batch_index, batch in enumerate(self._embedding_batches(texts), start=1):
                embeddings.extend(self._embed_document_batch(batch, batch_index=batch_index))

            logger.debug(f"批量嵌入完成, 维度: {len(embeddings[0])}")

            return embeddings

        except Exception as e:
            logger.error(f"批量嵌入失败: {e}")
            raise RuntimeError(f"批量嵌入失败: {e}") from e

    def _embedding_batches(self, texts: list[str]) -> list[list[str]]:
        return [
            texts[index : index + self.batch_size]
            for index in range(0, len(texts), self.batch_size)
        ]

    def _embed_document_batch(self, batch: list[str], *, batch_index: int) -> list[list[float]]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.embeddings.create(
                    model=self.model,
                    input=batch,
                    dimensions=self.dimensions,
                    encoding_format="float",
                )
                embeddings = [item.embedding for item in response.data]
                if len(embeddings) != len(batch):
                    raise RuntimeError(
                        f"Embedding 返回数量不一致: expected={len(batch)}, actual={len(embeddings)}"
                    )
                return [
                    self._validate_embedding(embedding, label=f"document batch {batch_index}")
                    for embedding in embeddings
                ]
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                sleep_seconds = min(2**attempt, 5)
                logger.warning(
                    f"文档嵌入 batch {batch_index} 失败，准备重试 "
                    f"({attempt + 1}/{self.max_retries}): {exc}"
                )
                time.sleep(sleep_seconds)
        raise RuntimeError(f"文档嵌入 batch {batch_index} 失败: {last_error}") from last_error

    def embed_query(self, text: str) -> list[float]:
        """
        嵌入单个查询文本 (LangChain 标准接口)

        Args:
            text: 查询文本

        Returns:
            List[float]: 嵌入向量
        """
        if not text or not text.strip():
            raise ValueError("查询文本不能为空")

        logger.debug(f"嵌入查询, 长度: {len(text)} 字符")
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.embeddings.create(
                    model=self.model,
                    input=text,
                    dimensions=self.dimensions,
                    encoding_format="float",
                )
                if len(response.data) != 1:
                    raise RuntimeError(
                        f"查询 Embedding 返回数量不一致: expected=1, actual={len(response.data)}"
                    )
                embedding = self._validate_embedding(response.data[0].embedding, label="query")
                logger.debug(f"查询嵌入完成, 维度: {len(embedding)}")
                return embedding
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                sleep_seconds = min(2**attempt, 5)
                logger.warning(f"查询嵌入失败，准备重试 ({attempt + 1}/{self.max_retries}): {exc}")
                time.sleep(sleep_seconds)
        logger.error(f"查询嵌入失败: {last_error}")
        raise RuntimeError(f"查询嵌入失败: {last_error}") from last_error

    def _validate_embedding(self, embedding: object, *, label: str) -> list[float]:
        """Validate provider output before Milvus sees it."""
        if isinstance(embedding, str) or not isinstance(embedding, Iterable):
            raise RuntimeError(f"{label} Embedding 返回格式无效")
        try:
            values = cast(Iterable[str | SupportsFloat | SupportsIndex], embedding)
            vector = [float(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{label} Embedding 包含非数值元素") from exc
        if len(vector) != self.dimensions:
            raise RuntimeError(
                f"{label} Embedding 维度不一致: expected={self.dimensions}, actual={len(vector)}"
            )
        if not all(math.isfinite(value) for value in vector):
            raise RuntimeError(f"{label} Embedding 包含 NaN 或 Infinity")
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 1e-12:
            raise RuntimeError(f"{label} Embedding 是零向量")
        return [value / norm for value in vector]


class LazyDashScopeEmbeddings(Embeddings):
    """Lazily create DashScopeEmbeddings on first real embedding call."""

    def __init__(self) -> None:
        self._service: DashScopeEmbeddings | None = None
        self._lock = RLock()

    def _get_service(self) -> DashScopeEmbeddings:
        if self._service is None:
            with self._lock:
                if self._service is None:
                    self._service = DashScopeEmbeddings(
                        api_key=config.dashscope_api_key,
                        model=config.dashscope_embedding_model,
                        dimensions=config.dashscope_embedding_dimensions,
                        base_url=config.dashscope_api_base,
                    )
        return self._service

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed documents after lazy client initialization."""
        return self._get_service().embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        """Embed a query after lazy client initialization."""
        return self._get_service().embed_query(text)


vector_embedding_service = LazyDashScopeEmbeddings()
