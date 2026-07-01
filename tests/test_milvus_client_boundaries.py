"""Milvus client safety boundary tests."""

import pytest

from app.core import milvus_client


def test_dimension_mismatch_does_not_drop_collection_by_default(monkeypatch) -> None:
    manager = milvus_client.MilvusClientManager()
    dropped: list[str] = []

    monkeypatch.setattr(milvus_client.config, "milvus_recreate_on_dimension_mismatch", False)
    monkeypatch.setattr(milvus_client.utility, "drop_collection", lambda name: dropped.append(name))

    with pytest.raises(RuntimeError, match="已阻止自动删除 collection"):
        manager._handle_vector_dimension_mismatch(768)

    assert dropped == []


def test_dimension_mismatch_can_recreate_collection_when_explicitly_enabled(monkeypatch) -> None:
    manager = milvus_client.MilvusClientManager()
    dropped: list[str] = []
    created: list[bool] = []

    monkeypatch.setattr(milvus_client.config, "milvus_recreate_on_dimension_mismatch", True)
    monkeypatch.setattr(milvus_client.utility, "drop_collection", lambda name: dropped.append(name))
    monkeypatch.setattr(manager, "_create_collection", lambda: created.append(True))

    manager._handle_vector_dimension_mismatch(768)

    assert dropped == [manager.COLLECTION_NAME]
    assert created == [True]
