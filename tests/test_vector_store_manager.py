"""VectorStore manager boundary tests."""

import pytest
from langchain_core.documents import Document

from app.services import vector_store_manager as vector_store_module


class FakeDeleteResult:
    delete_count = 3


class FakeCollection:
    def __init__(self) -> None:
        self.expr = ""
        self.flushed = False

    def delete(self, expr: str, *, timeout: float | None = None) -> FakeDeleteResult:
        self.expr = expr
        return FakeDeleteResult()

    def flush(self, *, timeout: float | None = None) -> None:
        self.flushed = True


def test_delete_by_source_escapes_milvus_metadata_expression(monkeypatch) -> None:
    collection = FakeCollection()
    manager = vector_store_module.VectorStoreManager()

    monkeypatch.setattr(vector_store_module.milvus_manager, "connect", lambda: object())
    monkeypatch.setattr(vector_store_module.milvus_manager, "get_collection", lambda: collection)

    deleted = manager.delete_by_source('/tmp/runbook"quoted\\path.md')

    assert deleted == 3
    assert collection.expr == (
        'metadata["_source_id"] == "/tmp/runbook\\"quoted/path.md" '
        'or metadata["_source"] == "/tmp/runbook\\"quoted\\\\path.md"'
    )


def test_add_documents_uses_stable_vector_ids(monkeypatch) -> None:
    captured: dict[str, object] = {}
    manager = vector_store_module.VectorStoreManager()

    class FakeVectorStore:
        class FakeClient:
            def __init__(self) -> None:
                self.deleted_ids = []

            def flush(self, *, collection_name, timeout):
                captured["flushed"] = (collection_name, timeout)

            def delete(self, *, collection_name, ids, timeout):
                self.deleted_ids.append(ids)
                return FakeDeleteResult()

        client = FakeClient()

        def upsert(self, documents, ids, batch_size, timeout):
            captured["documents"] = documents
            captured["ids"] = ids
            captured["batch_size"] = batch_size
            captured["timeout"] = timeout

    documents = [
        Document(
            page_content="Redis timeout runbook",
            metadata={
                "_source": "/docs/redis.md",
                "_document_hash": "doc-hash",
                "_chunk_id": "redis.md#0001",
                "_chunk_hash": "chunk-hash",
            },
        )
    ]

    monkeypatch.setattr(manager, "_ensure_vector_store", lambda: FakeVectorStore())

    first_ids = manager.add_documents(documents)
    second_ids = manager.add_documents(
        [
            Document(
                page_content="Redis timeout runbook",
                metadata={
                    "_source": "/docs/redis.md",
                    "_document_hash": "doc-hash",
                    "_chunk_id": "redis.md#0001",
                    "_chunk_hash": "chunk-hash",
                },
            )
        ]
    )

    assert first_ids == second_ids
    assert first_ids[0].startswith("vec-")
    assert captured["ids"] == second_ids
    assert captured["flushed"][0] == "biz"
    assert documents[0].metadata["_vector_id"] == first_ids[0]


def test_add_documents_upserts_when_collection_exists(monkeypatch) -> None:
    captured: dict[str, object] = {}
    manager = vector_store_module.VectorStoreManager()

    class FakeClient:
        def has_collection(self, collection_name: str) -> bool:
            captured["collection_name"] = collection_name
            return True

        def flush(self, *, collection_name: str, timeout: float) -> None:
            captured["flushed"] = collection_name

    class FakeVectorStore:
        collection_name = "biz"
        client = FakeClient()

        def upsert(self, ids, documents, batch_size, timeout):
            captured["upsert_ids"] = ids
            captured["upsert_documents"] = documents
            captured["batch_size"] = batch_size
            captured["timeout"] = timeout

        def add_documents(self, documents, ids):
            raise AssertionError("existing collection should use upsert")

    documents = [
        Document(
            page_content="Redis timeout runbook",
            metadata={"_source": "/docs/redis.md", "_chunk_id": "redis.md#0001"},
        )
    ]

    monkeypatch.setattr(manager, "_ensure_vector_store", lambda: FakeVectorStore())

    result_ids = manager.add_documents(documents)

    assert captured["upsert_ids"] == result_ids
    assert captured["upsert_documents"] == documents
    assert captured["batch_size"] == len(documents)
    assert captured["timeout"] == vector_store_module.config.milvus_timeout / 1000
    assert captured["flushed"] == "biz"


def test_delete_by_source_except_ids_keeps_current_batch(monkeypatch) -> None:
    collection = FakeCollection()
    manager = vector_store_module.VectorStoreManager()

    monkeypatch.setattr(vector_store_module.milvus_manager, "connect", lambda: object())
    monkeypatch.setattr(vector_store_module.milvus_manager, "get_collection", lambda: collection)

    deleted = manager.delete_by_source_except_ids('/tmp/runbook"quoted.md', ["vec-b", "vec-a"])

    assert deleted == 3
    assert collection.expr == (
        '(metadata["_source_id"] == "/tmp/runbook\\"quoted.md" '
        'or metadata["_source"] == "/tmp/runbook\\"quoted.md") '
        'and id not in ["vec-a", "vec-b"]'
    )
    assert collection.flushed is True


def test_delete_by_source_uses_canonical_identity_for_known_roots(monkeypatch) -> None:
    collection = FakeCollection()
    manager = vector_store_module.VectorStoreManager()
    monkeypatch.setattr(vector_store_module.milvus_manager, "connect", lambda: object())
    monkeypatch.setattr(vector_store_module.milvus_manager, "get_collection", lambda: collection)

    manager.delete_by_source("C:/repo/docs/knowledge-base/redis.md")

    assert collection.expr == (
        'metadata["_source_id"] == "docs/knowledge-base/redis.md" '
        'or metadata["_source"] == "C:/repo/docs/knowledge-base/redis.md"'
    )


def test_vector_id_is_stable_across_deployment_roots() -> None:
    left = Document(
        page_content="same content",
        metadata={
            "_source": "C:/repo/docs/knowledge-base/redis.md",
            "_source_id": "docs/knowledge-base/redis.md",
            "_chunk_id": "redis.md#0001",
            "_document_hash": "document-hash",
            "_chunk_hash": "chunk-hash",
        },
    )
    right = Document(
        page_content="same content",
        metadata={
            "_source": "/srv/app/docs/knowledge-base/redis.md",
            "_source_id": "docs/knowledge-base/redis.md",
            "_chunk_id": "redis.md#0001",
            "_document_hash": "document-hash",
            "_chunk_hash": "chunk-hash",
        },
    )

    assert vector_store_module.build_vector_document_id(
        left
    ) == vector_store_module.build_vector_document_id(right)


def test_vector_id_changes_when_chunk_version_changes() -> None:
    old = Document(
        page_content="old content",
        metadata={
            "_source_id": "docs/knowledge-base/redis.md",
            "_chunk_id": "redis.md#0001",
            "_document_hash": "old-document",
            "_chunk_hash": "old-chunk",
        },
    )
    new = Document(
        page_content="new content",
        metadata={
            "_source_id": "docs/knowledge-base/redis.md",
            "_chunk_id": "redis.md#0001",
            "_document_hash": "new-document",
            "_chunk_hash": "new-chunk",
        },
    )

    assert vector_store_module.build_vector_document_id(
        old
    ) != vector_store_module.build_vector_document_id(new)


def test_add_documents_fails_when_flush_cannot_confirm_visibility(monkeypatch) -> None:
    manager = vector_store_module.VectorStoreManager()
    captured: dict[str, object] = {}

    class FakeVectorStore:
        class FakeClient:
            def delete(self, *, collection_name, ids, timeout):
                captured["deleted"] = (collection_name, ids, timeout)
                return FakeDeleteResult()

            def flush(self, *, collection_name, timeout):
                captured["flush_calls"] = int(captured.get("flush_calls", 0)) + 1
                raise RuntimeError("flush unavailable")

        client = FakeClient()

        def upsert(self, ids, documents, batch_size, timeout) -> None:
            return None

    monkeypatch.setattr(manager, "_ensure_vector_store", lambda: FakeVectorStore())

    with pytest.raises(RuntimeError, match="flush"):
        manager.add_documents(
            [
                Document(
                    page_content="Redis timeout",
                    metadata={"_source_id": "uploads/redis.md", "_chunk_id": "redis.md#0001"},
                )
            ]
        )

    deleted = captured["deleted"]
    assert isinstance(deleted, tuple)
    assert deleted[0] == "biz"
    assert len(deleted[1]) == 1


def test_add_documents_compensates_known_ids_when_upsert_raises(monkeypatch) -> None:
    manager = vector_store_module.VectorStoreManager()
    captured: dict[str, object] = {}

    class FakeClient:
        def delete(self, *, collection_name, ids, timeout):
            captured["deleted"] = (collection_name, ids, timeout)
            return FakeDeleteResult()

        def flush(self, *, collection_name, timeout):
            captured["flushed"] = (collection_name, timeout)

    class FakeVectorStore:
        client = FakeClient()

        def upsert(self, ids, documents, batch_size, timeout) -> None:
            raise RuntimeError("partial upsert")

    monkeypatch.setattr(manager, "_ensure_vector_store", lambda: FakeVectorStore())

    with pytest.raises(RuntimeError, match="partial upsert"):
        manager.add_documents(
            [
                Document(
                    page_content="Redis timeout",
                    metadata={
                        "_source_id": "uploads/redis.md",
                        "_chunk_id": "redis.md#0001",
                        "_document_hash": "doc",
                        "_chunk_hash": "chunk",
                    },
                )
            ]
        )

    deleted = captured["deleted"]
    flushed = captured["flushed"]
    assert isinstance(deleted, tuple)
    assert isinstance(flushed, tuple)
    assert deleted[0] == "biz"
    assert len(deleted[1]) == 1
    assert flushed[0] == "biz"


def test_delete_by_ids_builds_bounded_compensation_expression(monkeypatch) -> None:
    collection = FakeCollection()
    manager = vector_store_module.VectorStoreManager()
    monkeypatch.setattr(vector_store_module.milvus_manager, "connect", lambda: object())
    monkeypatch.setattr(vector_store_module.milvus_manager, "get_collection", lambda: collection)

    deleted = manager.delete_by_ids(["vec-b", "vec-a", "vec-a"], raise_on_error=True)

    assert deleted == 3
    assert collection.expr == 'id in ["vec-a", "vec-b"]'
    assert collection.flushed is True


def test_similarity_search_propagates_backend_failure(monkeypatch) -> None:
    manager = vector_store_module.VectorStoreManager()

    class FailingVectorStore:
        def similarity_search(self, query, k, **kwargs):
            raise RuntimeError("milvus search failed")

    monkeypatch.setattr(manager, "_ensure_vector_store", lambda: FailingVectorStore())

    with pytest.raises(RuntimeError, match="milvus search failed"):
        manager.similarity_search("Redis timeout")


@pytest.mark.parametrize(
    ("query", "k"),
    [("", 1), ("   ", 1), ("x" * 8001, 1), ("Redis", 0), ("Redis", True)],
)
def test_similarity_search_rejects_invalid_inputs_before_initializing_store(
    monkeypatch,
    query,
    k,
) -> None:
    manager = vector_store_module.VectorStoreManager()
    monkeypatch.setattr(
        manager,
        "_ensure_vector_store",
        lambda: pytest.fail("invalid input must not initialize Milvus"),
    )

    with pytest.raises(ValueError):
        manager.similarity_search(query, k=k)


def test_similarity_search_rejects_non_string_expr_before_initializing_store(monkeypatch) -> None:
    manager = vector_store_module.VectorStoreManager()
    monkeypatch.setattr(
        manager,
        "_ensure_vector_store",
        lambda: pytest.fail("invalid input must not initialize Milvus"),
    )

    with pytest.raises(TypeError, match="expr"):
        manager.similarity_search("Redis", expr=1)  # type: ignore[arg-type]


def test_similarity_search_rejects_excessive_k_before_initializing_store(monkeypatch) -> None:
    manager = vector_store_module.VectorStoreManager()
    monkeypatch.setattr(
        manager,
        "_ensure_vector_store",
        lambda: pytest.fail("invalid input must not initialize Milvus"),
    )

    with pytest.raises(ValueError, match="不能超过"):
        manager.similarity_search("Redis", k=501)


def test_scored_similarity_search_uses_validated_manager_boundary(monkeypatch) -> None:
    manager = vector_store_module.VectorStoreManager()
    captured: dict[str, object] = {}
    document = Document(page_content="Redis", metadata={})

    class FakeVectorStore:
        def similarity_search_with_score(self, query, k, **kwargs):
            captured.update({"query": query, "k": k, **kwargs})
            return [(document, 0.2)]

    monkeypatch.setattr(manager, "_ensure_vector_store", lambda: FakeVectorStore())

    result = manager.similarity_search_with_score(" Redis ", k=2, expr='metadata["x"] == 1')

    assert result == [(document, 0.2)]
    assert captured == {
        "query": "Redis",
        "k": 2,
        "expr": 'metadata["x"] == 1',
    }
