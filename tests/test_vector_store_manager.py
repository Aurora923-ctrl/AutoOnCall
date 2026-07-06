"""VectorStore manager boundary tests."""

from langchain_core.documents import Document

from app.services import vector_store_manager as vector_store_module


class FakeDeleteResult:
    delete_count = 3


class FakeCollection:
    def __init__(self) -> None:
        self.expr = ""

    def delete(self, expr: str) -> FakeDeleteResult:
        self.expr = expr
        return FakeDeleteResult()


def test_delete_by_source_escapes_milvus_metadata_expression(monkeypatch) -> None:
    collection = FakeCollection()
    manager = vector_store_module.VectorStoreManager()

    monkeypatch.setattr(vector_store_module.milvus_manager, "connect", lambda: object())
    monkeypatch.setattr(vector_store_module.milvus_manager, "get_collection", lambda: collection)

    deleted = manager.delete_by_source('/tmp/runbook"quoted\\path.md')

    assert deleted == 3
    assert collection.expr == 'metadata["_source"] == "/tmp/runbook\\"quoted\\\\path.md"'


def test_add_documents_uses_stable_vector_ids(monkeypatch) -> None:
    captured: dict[str, object] = {}
    manager = vector_store_module.VectorStoreManager()

    class FakeVectorStore:
        def add_documents(self, documents, ids):
            captured["documents"] = documents
            captured["ids"] = ids
            return ids

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
    assert documents[0].metadata["_vector_id"] == first_ids[0]


def test_add_documents_upserts_when_collection_exists(monkeypatch) -> None:
    captured: dict[str, object] = {}
    manager = vector_store_module.VectorStoreManager()

    class FakeClient:
        def has_collection(self, collection_name: str) -> bool:
            captured["collection_name"] = collection_name
            return True

    class FakeVectorStore:
        collection_name = "biz"
        client = FakeClient()

        def upsert(self, ids, documents):
            captured["upsert_ids"] = ids
            captured["upsert_documents"] = documents

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

    assert captured["collection_name"] == "biz"
    assert captured["upsert_ids"] == result_ids
    assert captured["upsert_documents"] == documents


def test_delete_by_source_except_ids_keeps_current_batch(monkeypatch) -> None:
    collection = FakeCollection()
    manager = vector_store_module.VectorStoreManager()

    monkeypatch.setattr(vector_store_module.milvus_manager, "connect", lambda: object())
    monkeypatch.setattr(vector_store_module.milvus_manager, "get_collection", lambda: collection)

    deleted = manager.delete_by_source_except_ids('/tmp/runbook"quoted.md', ["vec-b", "vec-a"])

    assert deleted == 3
    assert collection.expr == (
        'metadata["_source"] == "/tmp/runbook\\"quoted.md" and id not in ["vec-a", "vec-b"]'
    )
