"""Tests for the local lexical RAG index."""

import json
import subprocess
import sys

import pytest
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


@pytest.mark.parametrize(
    ("query", "top_k"),
    [("", 1), ("   ", 1), ("Redis", 0), ("Redis", True)],
)
def test_lexical_index_rejects_invalid_search_inputs_without_reading_index(
    tmp_path,
    query,
    top_k,
) -> None:
    service = LexicalIndexService(tmp_path / "lexical.json")

    with pytest.raises(ValueError):
        service.search(query, top_k=top_k)


def test_lexical_index_rejects_overlong_query_without_reading_index(tmp_path) -> None:
    service = LexicalIndexService(tmp_path / "lexical.json")

    with pytest.raises(ValueError, match="8000"):
        service.search("x" * 8001, top_k=1)


def test_lexical_index_metadata_filter_preserves_scalar_types(tmp_path) -> None:
    service = LexicalIndexService(tmp_path / "lexical.json")
    service.upsert_source(
        "docs/knowledge-base/redis.md",
        [
            Document(
                page_content="Redis timeout",
                metadata={
                    "_source": "docs/knowledge-base/redis.md",
                    "_chunk_id": "redis.md#0001",
                    "enabled": True,
                    "version": 1,
                },
            )
        ],
    )

    assert service.search("Redis", top_k=1, metadata_filter={"enabled": 1}) == []
    assert service.search("Redis", top_k=1, metadata_filter={"version": "1"}) == []


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


def test_lexical_index_can_replace_chunks_without_clearing_stale_marker(tmp_path) -> None:
    service = LexicalIndexService(tmp_path / "lexical.json")
    source = "docs/knowledge-base/redis.md"
    service.mark_source_stale(source, "indexing_in_progress")

    service.upsert_source(
        source,
        [
            Document(
                page_content="Redis maxclients updated runbook",
                metadata={"_source": source, "_chunk_id": "redis.md#0001"},
            )
        ],
        clear_stale=False,
    )

    assert service.is_source_stale(source) is True
    assert service.search("Redis maxclients", top_k=3) == []
    service.clear_source_stale(source)
    assert service.search("Redis maxclients", top_k=3)


def test_lexical_index_replaces_source_and_clears_stale_in_one_write(
    monkeypatch,
    tmp_path,
) -> None:
    service = LexicalIndexService(tmp_path / "lexical.json")
    source = "docs/knowledge-base/redis.md"
    service.mark_source_stale(source, "indexing_in_progress")
    save_calls = {"count": 0}
    original_save = service._save_index

    def recording_save(payload) -> None:
        save_calls["count"] += 1
        original_save(payload)

    monkeypatch.setattr(service, "_save_index", recording_save)

    service.replace_source_and_clear_stale(
        source,
        [
            Document(
                page_content="Redis maxclients updated runbook",
                metadata={"_source": source, "_chunk_id": "redis.md#0001"},
            )
        ],
    )

    assert save_calls["count"] == 1
    assert service.is_source_stale(source) is False
    assert service.search("Redis maxclients", top_k=3)


def test_lexical_index_preserves_term_frequency_for_bm25_scoring(tmp_path) -> None:
    service = LexicalIndexService(tmp_path / "lexical.json")
    service.upsert_source(
        "docs/knowledge-base/repeated.md",
        [
            Document(
                page_content="timeout timeout timeout redis",
                metadata={"_chunk_id": "repeated.md#0001"},
            )
        ],
    )
    service.upsert_source(
        "docs/knowledge-base/single.md",
        [
            Document(
                page_content="timeout redis connection latency",
                metadata={"_chunk_id": "single.md#0001"},
            )
        ],
    )

    hits = service.search("timeout", top_k=2)

    assert [item[0].metadata["_chunk_id"] for item in hits] == [
        "repeated.md#0001",
        "single.md#0001",
    ]
    assert hits[0][1] > hits[1][1]


def test_lexical_source_identity_survives_deployment_root_changes(tmp_path) -> None:
    service = LexicalIndexService(tmp_path / "lexical.json")
    old_source = "C:/repo/docs/knowledge-base/redis.md"
    new_source = "/srv/app/docs/knowledge-base/redis.md"
    service.upsert_source(
        old_source,
        [
            Document(
                page_content="old Redis content",
                metadata={"_source": old_source, "_chunk_id": "redis.md#0001"},
            )
        ],
    )

    service.mark_source_stale(new_source, "indexing_in_progress")

    assert service.is_source_stale(old_source) is True
    assert service.search("Redis", top_k=3) == []

    service.replace_source_and_clear_stale(
        new_source,
        [
            Document(
                page_content="new Redis content",
                metadata={"_source": new_source, "_chunk_id": "redis.md#0001"},
            )
        ],
    )

    payload = json.loads((tmp_path / "lexical.json").read_text(encoding="utf-8"))
    assert [chunk["source_path"] for chunk in payload["chunks"]] == [new_source]
    assert payload["chunks"][0]["metadata"]["_source_id"] == "docs/knowledge-base/redis.md"
    assert service.is_source_stale(old_source) is False


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


def test_lexical_index_serializes_cross_process_updates(tmp_path) -> None:
    index_path = tmp_path / "lexical.json"
    worker = """
import sys
from langchain_core.documents import Document
from app.services.lexical_index_service import LexicalIndexService

path, source = sys.argv[1:3]
LexicalIndexService(path).upsert_source(
    source,
    [Document(page_content=source, metadata={"_source": source, "_chunk_id": source + "#0001"})],
)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(index_path), source],
            cwd=tmp_path,
        )
        for source in ("a.md", "b.md")
    ]

    assert [process.wait(timeout=30) for process in processes] == [0, 0]
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert {chunk["source_path"] for chunk in payload["chunks"]} == {"a.md", "b.md"}


def test_lexical_index_does_not_overwrite_corrupt_state(tmp_path) -> None:
    index_path = tmp_path / "lexical.json"
    index_path.write_text("{broken", encoding="utf-8")
    service = LexicalIndexService(index_path)

    with pytest.raises(RuntimeError, match="读取本地词法索引失败"):
        service.upsert_source(
            "redis.md",
            [Document(page_content="Redis", metadata={"_chunk_id": "redis.md#0001"})],
        )

    assert index_path.read_text(encoding="utf-8") == "{broken"


def test_lexical_index_restores_source_snapshot_after_failed_transaction(tmp_path) -> None:
    index_path = tmp_path / "lexical.json"
    service = LexicalIndexService(index_path)
    source = "docs/knowledge-base/redis.md"
    old = Document(
        page_content="old Redis runbook",
        metadata={
            "_source": source,
            "_source_id": source,
            "_file_name": "redis.md",
            "_chunk_id": "redis.md#0001",
            "_document_hash": "old-doc",
            "_chunk_hash": "old-chunk",
        },
    )
    service.upsert_source(source, [old])
    snapshot = service.snapshot_source_state(source)
    service.upsert_source(
        source,
        [
            Document(
                page_content="new Redis runbook",
                metadata={
                    "_source": source,
                    "_source_id": source,
                    "_file_name": "redis.md",
                    "_chunk_id": "redis.md#0002",
                    "_document_hash": "new-doc",
                    "_chunk_hash": "new-chunk",
                },
            )
        ],
    )

    service.restore_source_state(source, snapshot)

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert [chunk["content"] for chunk in payload["chunks"]] == ["old Redis runbook"]


def test_lexical_index_restores_complete_snapshot(tmp_path) -> None:
    index_path = tmp_path / "lexical.json"
    service = LexicalIndexService(index_path)
    service.upsert_source(
        "docs/knowledge-base/redis.md",
        [Document(page_content="old Redis runbook", metadata={"_chunk_id": "redis.md#0001"})],
    )
    snapshot = service.snapshot_index_state()

    service.replace_all_sources(
        {
            "docs/knowledge-base/mysql.md": [
                Document(page_content="new MySQL runbook", metadata={"_chunk_id": "mysql.md#0001"})
            ]
        }
    )
    service.restore_index_state(snapshot)

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert [chunk["source_path"] for chunk in payload["chunks"]] == [
        "docs/knowledge-base/redis.md"
    ]
