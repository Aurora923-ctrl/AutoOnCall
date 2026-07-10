"""Tests for the local lexical RAG index."""

from langchain_core.documents import Document

from app.services.lexical_index_service import LexicalIndexService


def test_lexical_index_upsert_search_filter_and_delete(tmp_path) -> None:
    service = LexicalIndexService(tmp_path / "lexical.json")
    service.upsert_source(
        "docs/knowledge-base/redis.md",
        [
            Document(
                page_content="Redis maxclients connection timeout runbook",
                metadata={
                    "_source": "docs/knowledge-base/redis.md",
                    "_file_name": "redis.md",
                    "_doc_id": "docs/knowledge-base/redis.md",
                    "_chunk_id": "redis.md#0001",
                    "_document_version": "v1",
                },
            )
        ],
    )

    hits = service.search("Redis maxclients timeout", top_k=3)
    assert len(hits) == 1
    assert hits[0][0].metadata["_chunk_id"] == "redis.md#0001"
    assert hits[0][1] > 0

    assert (
        service.search(
            "Redis maxclients timeout",
            top_k=3,
            metadata_filter={"_document_version": "missing"},
        )
        == []
    )

    assert service.delete_source("docs/knowledge-base/redis.md") == 1
    assert service.search("Redis maxclients timeout", top_k=3) == []


def test_lexical_index_stale_source_is_excluded_until_reindexed(tmp_path) -> None:
    service = LexicalIndexService(tmp_path / "lexical.json")
    source = "docs/knowledge-base/redis.md"
    document = Document(
        page_content="Redis maxclients connection timeout runbook",
        metadata={"_source": source, "_chunk_id": "redis.md#0001"},
    )
    service.upsert_source(source, [document])

    service.mark_source_stale(source, "new index failed")

    assert service.is_source_stale(source) is True
    assert service.search("Redis maxclients timeout", top_k=3) == []

    service.upsert_source(source, [document])

    assert service.is_source_stale(source) is False
    assert service.search("Redis maxclients timeout", top_k=3)


def test_lexical_index_writes_json_atomically_without_temp_leftovers(tmp_path) -> None:
    index_path = tmp_path / "lexical.json"
    service = LexicalIndexService(index_path)

    service.upsert_source(
        "docs/knowledge-base/cpu.md",
        [
            Document(
                page_content="CPU high usage runbook",
                metadata={"_source": "docs/knowledge-base/cpu.md", "_chunk_id": "cpu.md#0001"},
            )
        ],
    )

    assert index_path.exists()
    assert service.search("CPU usage", top_k=1)
    assert list(tmp_path.glob(".lexical.json.*.tmp")) == []
