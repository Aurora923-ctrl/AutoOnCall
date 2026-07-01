"""VectorStore manager boundary tests."""

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
