"""Milvus client safety boundary tests."""

import pytest
from pymilvus import CollectionSchema, DataType, FieldSchema

from app.core import milvus_client


def test_dimension_mismatch_does_not_drop_collection_by_default(monkeypatch) -> None:
    manager = milvus_client.MilvusClientManager()
    dropped: list[str] = []

    monkeypatch.setattr(milvus_client.config, "milvus_recreate_on_dimension_mismatch", False)
    monkeypatch.setattr(
        milvus_client.utility,
        "drop_collection",
        lambda name, **_kwargs: dropped.append(name),
    )

    with pytest.raises(RuntimeError, match="已阻止自动删除 collection"):
        manager._handle_vector_dimension_mismatch(768)

    assert dropped == []


def test_dimension_mismatch_can_recreate_collection_when_explicitly_enabled(monkeypatch) -> None:
    manager = milvus_client.MilvusClientManager()
    dropped: list[str] = []
    created: list[bool] = []

    monkeypatch.setattr(milvus_client.config, "milvus_recreate_on_dimension_mismatch", True)
    monkeypatch.setattr(
        milvus_client.utility,
        "drop_collection",
        lambda name, **_kwargs: dropped.append(name),
    )
    monkeypatch.setattr(manager, "_create_collection", lambda: created.append(True))

    manager._handle_vector_dimension_mismatch(768)

    assert dropped == [manager.COLLECTION_NAME]
    assert created == [True]


def test_health_check_uses_collection_probe(monkeypatch) -> None:
    manager = milvus_client.MilvusClientManager()
    manager._client = object()  # type: ignore[assignment]
    probed: list[str] = []

    monkeypatch.setattr(
        milvus_client.connections,
        "has_connection",
        lambda alias: alias == manager.CONNECTION_ALIAS,
    )
    monkeypatch.setattr(
        milvus_client.utility,
        "has_collection",
        lambda name, **_kwargs: probed.append(name) or True,
    )

    assert manager.health_check() is True
    assert probed == [manager.COLLECTION_NAME]


def test_health_check_closes_stale_client_when_probe_fails(monkeypatch) -> None:
    manager = milvus_client.MilvusClientManager()
    manager._client = object()  # type: ignore[assignment]
    disconnected: list[str] = []

    def fail_probe(name: str) -> bool:
        raise RuntimeError(f"{name} unavailable")

    monkeypatch.setattr(
        milvus_client.connections,
        "has_connection",
        lambda alias: alias == manager.CONNECTION_ALIAS,
    )
    monkeypatch.setattr(milvus_client.connections, "disconnect", disconnected.append)
    monkeypatch.setattr(milvus_client.utility, "has_collection", fail_probe)

    assert manager.health_check() is False
    assert manager._client is None
    assert disconnected == [manager.CONNECTION_ALIAS]


def test_readiness_check_does_not_create_or_load_collection(monkeypatch) -> None:
    manager = milvus_client.MilvusClientManager()
    calls: list[tuple[str, object]] = []

    class ProbeClient:
        def __init__(self, *, uri: str, timeout: float) -> None:
            calls.append(("init", (uri, timeout)))

        def has_collection(self, *, collection_name: str, timeout: float) -> bool:
            calls.append(("has_collection", (collection_name, timeout)))
            return True

        def describe_collection(self, *, collection_name: str, timeout: float):
            calls.append(("describe_collection", (collection_name, timeout)))
            return {
                "auto_id": False,
                "enable_dynamic_field": False,
                "fields": [
                    {
                        "name": "id",
                        "type": int(DataType.VARCHAR),
                        "params": {"max_length": manager.ID_MAX_LENGTH},
                        "is_primary": True,
                    },
                    {
                        "name": "vector",
                        "type": int(DataType.FLOAT_VECTOR),
                        "params": {"dim": manager.vector_dim},
                    },
                    {
                        "name": "content",
                        "type": int(DataType.VARCHAR),
                        "params": {"max_length": manager.CONTENT_MAX_LENGTH},
                    },
                    {"name": "metadata", "type": int(DataType.JSON), "params": {}},
                ],
            }

        def list_indexes(self, *, collection_name: str):
            calls.append(("list_indexes", collection_name))
            return ["vector"]

        def describe_index(self, *, collection_name: str, index_name: str, timeout: float):
            calls.append(("describe_index", (collection_name, index_name, timeout)))
            return {
                "metric_type": manager.VECTOR_METRIC_TYPE,
                "index_type": manager.VECTOR_INDEX_TYPE,
                "params": {"nlist": manager.VECTOR_INDEX_NLIST},
                "state": "Finished",
                "pending_index_rows": 0,
            }

        def get_load_state(self, *, collection_name: str, timeout: float):
            calls.append(("get_load_state", (collection_name, timeout)))
            return {"state": "Loaded"}

        def query(
            self,
            *,
            collection_name: str,
            filter: str,
            output_fields: list[str],
            limit: int,
            timeout: float,
        ):
            calls.append(
                (
                    "query",
                    (collection_name, filter, output_fields, limit, timeout),
                )
            )
            return [
                {
                    "id": "vec-sample",
                    "metadata": {
                        "_source": "/docs/runbook.md",
                        "_source_id": "docs/runbook.md",
                        "_chunk_id": "runbook.md#0001",
                        "_document_hash": "doc-hash",
                        "_chunk_hash": "chunk-hash",
                        "_vector_id": "vec-sample",
                    },
                }
            ]

        def close(self) -> None:
            calls.append(("close", None))

    monkeypatch.setattr(milvus_client, "MilvusClient", ProbeClient)
    monkeypatch.setattr(
        manager,
        "_create_collection",
        lambda: pytest.fail("readiness must not create a collection"),
    )
    monkeypatch.setattr(
        manager,
        "_load_collection",
        lambda: pytest.fail("readiness must not load a collection"),
    )

    assert manager.readiness_check() is True
    assert [name for name, _ in calls] == [
        "init",
        "has_collection",
        "describe_collection",
        "list_indexes",
        "describe_index",
        "get_load_state",
        "query",
        "close",
    ]


@pytest.mark.parametrize(
    ("load_state", "index_state", "pending_rows"),
    [
        ({"state": "NotLoad"}, "Finished", 0),
        ({"state": "Loaded"}, "InProgress", 0),
        ({"state": "Loaded"}, "Finished", 4),
    ],
)
def test_readiness_runtime_state_rejects_unready_collection(
    load_state,
    index_state,
    pending_rows,
) -> None:
    manager = milvus_client.MilvusClientManager()

    with pytest.raises(RuntimeError):
        manager._validate_collection_runtime_state(
            load_state=load_state,
            index_description={
                "state": index_state,
                "pending_index_rows": pending_rows,
            },
        )


@pytest.mark.parametrize(
    "records",
    [
        [{"id": "vec-sample", "metadata": {}}],
        [
            {
                "id": "vec-sample",
                "metadata": {
                    "_source": "/docs/runbook.md",
                    "_source_id": "docs/runbook.md",
                    "_chunk_id": "runbook.md#0001",
                    "_document_hash": "doc-hash",
                    "_chunk_hash": "chunk-hash",
                    "_vector_id": "vec-other",
                },
            }
        ],
    ],
)
def test_readiness_rejects_incompatible_sample_metadata(records) -> None:
    manager = milvus_client.MilvusClientManager()

    with pytest.raises(RuntimeError, match="metadata 不兼容"):
        manager._validate_collection_sample_metadata(records)


def test_connect_retries_and_uses_default_alias(monkeypatch) -> None:
    manager = milvus_client.MilvusClientManager()
    attempts = {"count": 0}
    client_calls: list[dict[str, object]] = []

    def connect(**kwargs) -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ConnectionError("temporary")

    class ProbeClient:
        def __init__(self, **kwargs) -> None:
            client_calls.append(kwargs)

    monkeypatch.setattr(milvus_client.config, "milvus_connect_max_retries", 1)
    monkeypatch.setattr(milvus_client.config, "milvus_connect_retry_delay_seconds", 0)
    monkeypatch.setattr(milvus_client.connections, "connect", connect)
    monkeypatch.setattr(milvus_client.connections, "has_connection", lambda _alias: False)
    monkeypatch.setattr(milvus_client, "MilvusClient", ProbeClient)

    manager._connect_with_retry()

    assert attempts["count"] == 2
    assert client_calls[0]["alias"] == manager.CONNECTION_ALIAS
    assert client_calls[0]["timeout"] == milvus_client.config.milvus_timeout / 1000


def test_existing_collection_schema_rejects_missing_metadata_field() -> None:
    manager = milvus_client.MilvusClientManager()
    schema = CollectionSchema(
        fields=[
            FieldSchema(
                name="id",
                dtype=DataType.VARCHAR,
                max_length=100,
                is_primary=True,
            ),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=manager.vector_dim),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=8000),
        ]
    )

    with pytest.raises(RuntimeError, match="metadata"):
        manager._validate_collection_schema(schema)


def test_existing_collection_schema_rejects_incompatible_varchar_limits() -> None:
    manager = milvus_client.MilvusClientManager()
    schema = CollectionSchema(
        fields=[
            FieldSchema(
                name="id",
                dtype=DataType.VARCHAR,
                max_length=64,
                is_primary=True,
            ),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=manager.vector_dim),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=8000),
            FieldSchema(name="metadata", dtype=DataType.JSON),
        ]
    )

    with pytest.raises(RuntimeError, match="expected_max_length=100"):
        manager._validate_collection_schema(schema)
