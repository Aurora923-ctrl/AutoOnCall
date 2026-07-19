"""Milvus 客户端工厂模块"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock
from typing import Any

from loguru import logger
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
    MilvusException,
    connections,
    utility,
)

from app.config import config


class MilvusClientManager:
    """Milvus 客户端管理器"""

    COLLECTION_NAME: str = "biz"
    CONNECTION_ALIAS: str = "autooncall-schema"

    ID_MAX_LENGTH: int = 100

    CONTENT_MAX_LENGTH: int = 8000
    REQUIRED_METADATA_FIELDS: tuple[str, ...] = (
        "_source",
        "_source_id",
        "_chunk_id",
        "_document_hash",
        "_chunk_hash",
        "_vector_id",
    )

    DEFAULT_SHARD_NUMBER: int = 2
    VECTOR_INDEX_TYPE: str = "IVF_FLAT"
    VECTOR_METRIC_TYPE: str = "L2"
    VECTOR_INDEX_NLIST: int = 128

    def __init__(self) -> None:
        """初始化 Milvus 客户端管理器"""
        self._client: MilvusClient | None = None
        self._collection: Collection | None = None
        self._lock = RLock()

    @property
    def vector_dim(self) -> int:
        """Return the configured embedding dimension used by the collection schema."""
        return int(config.dashscope_embedding_dimensions)

    def connect(self) -> MilvusClient:
        """
        连接到 Milvus 服务器并初始化 collection

        Returns:
            MilvusClient: Milvus 客户端实例

        Raises:
            RuntimeError: 连接或初始化失败时抛出
        """
        if self._collection is not None and self._client is not None:
            logger.debug("Milvus 已连接，跳过重复 connect")
            return self._client

        with self._lock:
            if self._collection is not None and self._client is not None:
                logger.debug("Milvus 已连接，跳过重复 connect")
                return self._client

            try:
                logger.info(f"正在连接到 Milvus: {config.milvus_host}:{config.milvus_port}")

                self._connect_with_retry()

                if not self._collection_exists():
                    logger.info(f"collection '{self.COLLECTION_NAME}' 不存在，正在创建...")
                    self._create_collection()
                    logger.info(f"成功创建 collection '{self.COLLECTION_NAME}'")
                else:
                    logger.info(f"collection '{self.COLLECTION_NAME}' 已存在")
                    self._collection = Collection(
                        self.COLLECTION_NAME,
                        using=self.CONNECTION_ALIAS,
                    )

                    schema = self._collection.schema
                    vector_field = None
                    existing_dim = None
                    for field in schema.fields:
                        if field.name == "vector":
                            vector_field = field
                            break

                    if (
                        vector_field
                        and hasattr(vector_field, "params")
                        and "dim" in vector_field.params
                    ):
                        existing_dim = int(vector_field.params["dim"])
                        if existing_dim != self.vector_dim:
                            self._handle_vector_dimension_mismatch(existing_dim)
                        else:
                            logger.info(f"向量维度匹配: {self.vector_dim}")

                    self._validate_collection_schema(schema)
                    self._ensure_collection_index()

                self._load_collection()

                return self._client

            except MilvusException as e:
                logger.error(f"Milvus 操作失败: {e}")
                self.close()
                raise RuntimeError(f"Milvus 操作失败: {e}") from e
            except ConnectionError as e:
                logger.error(f"连接 Milvus 失败: {e}")
                self.close()
                raise RuntimeError(f"连接 Milvus 失败: {e}") from e
            except Exception as e:
                logger.error(f"连接 Milvus 失败: {e}")
                self.close()
                raise RuntimeError(f"连接 Milvus 失败: {e}") from e

    def _connect_with_retry(self) -> None:
        """Create both ORM and client connections with bounded retries."""
        max_retries = int(config.milvus_connect_max_retries)
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                connections.connect(
                    alias=self.CONNECTION_ALIAS,
                    host=config.milvus_host,
                    port=str(config.milvus_port),
                    timeout=config.milvus_timeout / 1000,
                )
                uri = f"http://{config.milvus_host}:{config.milvus_port}"
                self._client = MilvusClient(
                    uri=uri,
                    timeout=config.milvus_timeout / 1000,
                    alias=self.CONNECTION_ALIAS,
                )
                logger.info("成功连接到 Milvus")
                return
            except Exception as exc:
                last_error = exc
                try:
                    if connections.has_connection(self.CONNECTION_ALIAS):
                        connections.disconnect(self.CONNECTION_ALIAS)
                except Exception:
                    logger.warning("清理失败的 Milvus 连接尝试时出现异常")
                self._client = None
                if attempt >= max_retries:
                    break
                delay = float(config.milvus_connect_retry_delay_seconds) * (2**attempt)
                logger.warning(
                    f"连接 Milvus 失败，准备重试 ({attempt + 1}/{max_retries}): "
                    f"error_type={type(exc).__name__}"
                )
                if delay > 0:
                    time.sleep(delay)
        if last_error is None:
            raise RuntimeError("连接 Milvus 失败")
        raise last_error

    def _collection_exists(self) -> bool:
        """检查 collection 是否存在"""
        # pymilvus 的类型标注可能不准确，实际返回 bool
        result = utility.has_collection(
            self.COLLECTION_NAME,
            using=self.CONNECTION_ALIAS,
            timeout=config.milvus_timeout / 1000,
        )
        return bool(result)

    def _handle_vector_dimension_mismatch(self, existing_dim: int) -> None:
        """Handle an existing collection whose vector dimension is incompatible."""
        message = (
            f"检测到向量维度不匹配！collection '{self.COLLECTION_NAME}' 当前维度: "
            f"{existing_dim}, 配置维度: {self.vector_dim}"
        )
        logger.warning(message)

        if not config.milvus_recreate_on_dimension_mismatch:
            raise RuntimeError(
                f"{message}。为避免误删知识库数据，已阻止自动删除 collection；"
                "如确认是开发或演示环境需要重建，请设置 "
                "MILVUS_RECREATE_ON_DIMENSION_MISMATCH=true 后重启。"
            )

        logger.warning(
            "MILVUS_RECREATE_ON_DIMENSION_MISMATCH=true，允许删除并重建 "
            f"collection '{self.COLLECTION_NAME}'"
        )
        _ = utility.drop_collection(
            self.COLLECTION_NAME,
            using=self.CONNECTION_ALIAS,
            timeout=config.milvus_timeout / 1000,
        )
        self._create_collection()
        logger.info(f"成功重新创建 collection，维度: {self.vector_dim}")

    def _validate_collection_schema(self, schema: CollectionSchema) -> None:
        """Reject an existing collection that cannot satisfy the application contract."""
        fields = {field.name: field for field in schema.fields}
        required_types = {
            "id": DataType.VARCHAR,
            "vector": DataType.FLOAT_VECTOR,
            "content": DataType.VARCHAR,
            "metadata": DataType.JSON,
        }
        for name, expected_type in required_types.items():
            field = fields.get(name)
            if field is None or field.dtype != expected_type:
                raise RuntimeError(
                    f"collection '{self.COLLECTION_NAME}' schema 不兼容: "
                    f"field={name}, expected={expected_type}, "
                    f"actual={getattr(field, 'dtype', None)}"
                )
        if not bool(getattr(fields["id"], "is_primary", False)):
            raise RuntimeError(f"collection '{self.COLLECTION_NAME}' schema 不兼容: id 不是主键")
        if bool(getattr(fields["id"], "auto_id", False)):
            raise RuntimeError(
                f"collection '{self.COLLECTION_NAME}' schema 不兼容: id 不应启用 auto_id"
            )
        self._validate_varchar_length(fields["id"], self.ID_MAX_LENGTH)
        self._validate_varchar_length(fields["content"], self.CONTENT_MAX_LENGTH)
        if bool(getattr(schema, "enable_dynamic_field", False)):
            raise RuntimeError(
                f"collection '{self.COLLECTION_NAME}' schema 不兼容: dynamic field 已启用"
            )

    def _validate_collection_description(self, description: dict[str, Any]) -> None:
        """Validate schema data returned by the lightweight MilvusClient API."""
        fields = {
            str(field.get("name") or ""): field
            for field in description.get("fields", [])
            if isinstance(field, dict)
        }
        required_types = {
            "id": int(DataType.VARCHAR),
            "vector": int(DataType.FLOAT_VECTOR),
            "content": int(DataType.VARCHAR),
            "metadata": int(DataType.JSON),
        }
        for name, expected_type in required_types.items():
            field = fields.get(name)
            actual_type = field.get("type") if field else None
            if field is None or int(actual_type or -1) != expected_type:
                raise RuntimeError(
                    f"collection '{self.COLLECTION_NAME}' schema 不兼容: "
                    f"field={name}, expected={expected_type}, actual={actual_type}"
                )
        if not bool(fields["id"].get("is_primary")):
            raise RuntimeError(f"collection '{self.COLLECTION_NAME}' schema 不兼容: id 不是主键")
        if bool(description.get("auto_id")):
            raise RuntimeError(
                f"collection '{self.COLLECTION_NAME}' schema 不兼容: id 不应启用 auto_id"
            )
        if bool(description.get("enable_dynamic_field")):
            raise RuntimeError(
                f"collection '{self.COLLECTION_NAME}' schema 不兼容: dynamic field 已启用"
            )
        self._validate_description_varchar_length(fields["id"], self.ID_MAX_LENGTH)
        self._validate_description_varchar_length(fields["content"], self.CONTENT_MAX_LENGTH)
        vector_dim = int((fields["vector"].get("params") or {}).get("dim") or 0)
        if vector_dim != self.vector_dim:
            raise RuntimeError(
                f"collection '{self.COLLECTION_NAME}' 向量维度不兼容: "
                f"expected={self.vector_dim}, actual={vector_dim}"
            )

    def _validate_collection_runtime_state(
        self,
        *,
        load_state: dict[str, Any],
        index_description: dict[str, Any],
    ) -> None:
        """Reject a collection that exists but is not ready to serve indexed searches."""
        state = load_state.get("state")
        state_name = str(getattr(state, "name", state) or "").lower()
        if state_name != "loaded":
            raise RuntimeError(
                f"collection '{self.COLLECTION_NAME}' 未加载: actual={state_name or 'unknown'}"
            )

        index_state = str(index_description.get("state") or "").lower()
        if index_state and index_state not in {"finished", "finishedstate"}:
            raise RuntimeError(
                f"collection '{self.COLLECTION_NAME}' vector 索引未完成: "
                f"actual={index_description.get('state')}"
            )
        pending_rows = int(index_description.get("pending_index_rows") or 0)
        if pending_rows > 0:
            raise RuntimeError(
                f"collection '{self.COLLECTION_NAME}' vector 索引仍有待处理数据: "
                f"pending_index_rows={pending_rows}"
            )

    def _validate_collection_sample_metadata(self, records: list[dict[str, Any]]) -> None:
        """Reject a non-empty collection whose sampled rows violate the metadata contract."""
        for record in records:
            metadata = record.get("metadata")
            if not isinstance(metadata, dict):
                raise RuntimeError(
                    f"collection '{self.COLLECTION_NAME}' metadata 不兼容: metadata 不是对象"
                )
            missing = [name for name in self.REQUIRED_METADATA_FIELDS if not metadata.get(name)]
            if missing:
                raise RuntimeError(
                    f"collection '{self.COLLECTION_NAME}' metadata 不兼容: "
                    f"id={record.get('id')}, missing={','.join(missing)}"
                )
            if str(metadata.get("_vector_id")) != str(record.get("id")):
                raise RuntimeError(
                    f"collection '{self.COLLECTION_NAME}' metadata 不兼容: "
                    f"id={record.get('id')}, _vector_id={metadata.get('_vector_id')}"
                )

    def _validate_varchar_length(self, field: FieldSchema, expected: int) -> None:
        """Reject VARCHAR fields whose declared limits differ from the application contract."""
        actual = int(getattr(field, "params", {}).get("max_length") or 0)
        if actual != expected:
            raise RuntimeError(
                f"collection '{self.COLLECTION_NAME}' schema 不兼容: "
                f"field={field.name}, expected_max_length={expected}, actual={actual}"
            )

    def _validate_description_varchar_length(
        self,
        field: dict[str, Any],
        expected: int,
    ) -> None:
        actual = int((field.get("params") or {}).get("max_length") or 0)
        if actual != expected:
            raise RuntimeError(
                f"collection '{self.COLLECTION_NAME}' schema 不兼容: "
                f"field={field.get('name')}, expected_max_length={expected}, actual={actual}"
            )

    def _create_collection(self) -> None:
        """创建 biz collection"""
        fields = [
            FieldSchema(
                name="id",
                dtype=DataType.VARCHAR,
                max_length=self.ID_MAX_LENGTH,
                is_primary=True,
            ),
            FieldSchema(
                name="vector",
                dtype=DataType.FLOAT_VECTOR,
                dim=self.vector_dim,
            ),
            FieldSchema(
                name="content",
                dtype=DataType.VARCHAR,
                max_length=self.CONTENT_MAX_LENGTH,
            ),
            FieldSchema(
                name="metadata",
                dtype=DataType.JSON,
            ),
        ]

        schema = CollectionSchema(
            fields=fields,
            description="Business knowledge collection",
            enable_dynamic_field=False,
        )

        self._collection = Collection(
            name=self.COLLECTION_NAME,
            schema=schema,
            using=self.CONNECTION_ALIAS,
            num_shards=self.DEFAULT_SHARD_NUMBER,
        )

        self._create_index()

    def _create_index(self) -> None:
        """为 vector 字段创建索引"""
        if self._collection is None:
            raise RuntimeError("Collection 未初始化")

        index_params = {
            "metric_type": self.VECTOR_METRIC_TYPE,
            "index_type": self.VECTOR_INDEX_TYPE,
            "params": {"nlist": self.VECTOR_INDEX_NLIST},
        }

        _ = self._collection.create_index(
            field_name="vector",
            index_params=index_params,
        )

        logger.info("成功为 vector 字段创建索引")

    def _ensure_collection_index(self) -> None:
        """Create a missing vector index or reject an incompatible existing index."""
        if self._collection is None:
            raise RuntimeError("Collection 未初始化")
        indexes = list(self._collection.indexes)
        vector_indexes = [index for index in indexes if index.field_name == "vector"]
        if not vector_indexes:
            self._create_index()
            return
        if len(vector_indexes) != 1:
            raise RuntimeError(
                f"collection '{self.COLLECTION_NAME}' vector 索引数量不兼容: "
                f"actual={len(vector_indexes)}"
            )
        self._validate_index_description(dict(vector_indexes[0].params or {}))

    def _validate_index_description(self, description: dict[str, Any]) -> None:
        """Reject index settings that do not match retrieval score semantics."""
        metric_type = str(description.get("metric_type") or "").upper()
        index_type = str(description.get("index_type") or "").upper()
        params = description.get("params")
        index_params = params if isinstance(params, dict) else {}
        nlist = int(index_params.get("nlist") or 0)
        if (
            metric_type != self.VECTOR_METRIC_TYPE
            or index_type != self.VECTOR_INDEX_TYPE
            or nlist != self.VECTOR_INDEX_NLIST
        ):
            raise RuntimeError(
                f"collection '{self.COLLECTION_NAME}' vector 索引不兼容: "
                f"expected={self.VECTOR_INDEX_TYPE}/{self.VECTOR_METRIC_TYPE}/"
                f"nlist={self.VECTOR_INDEX_NLIST}, "
                f"actual={index_type}/{metric_type}/nlist={nlist}"
            )

    def _load_collection(self) -> None:
        """加载 collection 到内存"""
        if self._collection is None:
            self._collection = Collection(self.COLLECTION_NAME, using=self.CONNECTION_ALIAS)

        # 检查 collection 是否已加载（兼容多版本）
        try:
            load_state = utility.load_state(
                self.COLLECTION_NAME,
                using=self.CONNECTION_ALIAS,
                timeout=config.milvus_timeout / 1000,
            )
            # load_state 返回字符串或枚举，如 "Loaded" 或 "NotLoad"
            state_name = getattr(load_state, "name", str(load_state))
            if state_name != "Loaded":
                self._collection.load()
                logger.info(f"成功加载 collection '{self.COLLECTION_NAME}'")
            else:
                logger.info(f"Collection '{self.COLLECTION_NAME}' 已加载")
        except AttributeError:
            try:
                self._collection.load()
                logger.info(f"成功加载 collection '{self.COLLECTION_NAME}'")
            except MilvusException as e:
                error_msg = str(e).lower()
                if "already loaded" in error_msg or "loaded" in error_msg:
                    logger.info(f"Collection '{self.COLLECTION_NAME}' 已加载")
                else:
                    raise
        except Exception as e:
            logger.error(f"加载 collection 失败: {e}")
            raise

    def get_collection(self) -> Collection:
        """
        获取 collection 实例

        Returns:
            Collection: collection 实例

        Raises:
            RuntimeError: collection 未初始化时抛出
        """
        if self._collection is None:
            raise RuntimeError("Collection 未初始化，请先调用 connect()")
        return self._collection

    @contextmanager
    def collection_session(self) -> Iterator[Collection]:
        """Keep the shared ORM connection alive for one complete collection operation."""
        with self._lock:
            _ = self.connect()
            yield self.get_collection()

    def health_check(self) -> bool:
        """
        健康检查

        Returns:
            bool: True 表示健康，False 表示异常
        """
        try:
            if self._client is None:
                return False

            if not connections.has_connection(self.CONNECTION_ALIAS):
                self.close()
                return False

            if not bool(
                utility.has_collection(
                    self.COLLECTION_NAME,
                    using=self.CONNECTION_ALIAS,
                    timeout=config.milvus_timeout / 1000,
                )
            ):
                logger.warning(f"Milvus collection '{self.COLLECTION_NAME}' 不存在")
                self.close()
                return False

            return True

        except (MilvusException, ConnectionError) as e:
            logger.error(f"Milvus 健康检查失败: {e}")
            self.close()
            return False
        except Exception as e:
            logger.error(f"Milvus 健康检查失败: {e}")
            self.close()
            return False

    def readiness_check(self) -> bool:
        """Validate the live collection contract without creating, loading, or writing data."""
        client: MilvusClient | None = None
        try:
            uri = f"http://{config.milvus_host}:{config.milvus_port}"
            client = MilvusClient(uri=uri, timeout=config.milvus_timeout / 1000)
            if not bool(
                client.has_collection(
                    collection_name=self.COLLECTION_NAME,
                    timeout=config.milvus_timeout / 1000,
                )
            ):
                return False
            description = client.describe_collection(
                collection_name=self.COLLECTION_NAME,
                timeout=config.milvus_timeout / 1000,
            )
            self._validate_collection_description(description)
            indexes = client.list_indexes(collection_name=self.COLLECTION_NAME)
            if "vector" not in indexes:
                return False
            index_description = client.describe_index(
                collection_name=self.COLLECTION_NAME,
                index_name="vector",
                timeout=config.milvus_timeout / 1000,
            )
            self._validate_index_description(index_description)
            load_state = client.get_load_state(
                collection_name=self.COLLECTION_NAME,
                timeout=config.milvus_timeout / 1000,
            )
            self._validate_collection_runtime_state(
                load_state=load_state,
                index_description=index_description,
            )
            sample_records = client.query(
                collection_name=self.COLLECTION_NAME,
                filter="",
                output_fields=["id", "metadata"],
                limit=20,
                timeout=config.milvus_timeout / 1000,
            )
            self._validate_collection_sample_metadata(sample_records)
            return True
        except Exception as exc:
            logger.warning("Milvus readiness probe failed: error_type={}", type(exc).__name__)
            return False
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    logger.warning("Milvus readiness probe client close failed")

    def close(self) -> None:
        """关闭连接"""
        with self._lock:
            errors = []

            try:
                if self._collection is not None:
                    self._collection.release()
                    self._collection = None
            except Exception as e:
                errors.append(f"释放 collection 失败: {e}")

            try:
                if self._client is not None:
                    self._client.close()
            except Exception as e:
                errors.append(f"关闭 MilvusClient 失败: {e}")

            try:
                if connections.has_connection(self.CONNECTION_ALIAS):
                    connections.disconnect(self.CONNECTION_ALIAS)
            except Exception as e:
                errors.append(f"断开连接失败: {e}")

            self._client = None

            if errors:
                error_msg = "; ".join(errors)
                logger.error(f"关闭 Milvus 连接时出现错误: {error_msg}")
            else:
                logger.info("已关闭 Milvus 连接")

    def __enter__(self) -> "MilvusClientManager":
        """上下文管理器入口"""
        _ = self.connect()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object
    ) -> None:
        """上下文管理器退出"""
        self.close()


milvus_manager = MilvusClientManager()
