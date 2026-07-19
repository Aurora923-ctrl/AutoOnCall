"""Tests for legacy lexical-index identity migration."""

from scripts.maintenance.migrate_legacy_lexical_index import migrate_payload


def test_migrate_payload_adds_stable_source_identity_and_normalizes_stale_keys() -> None:
    source = "C:/repo/aiops-docs/redis.md"
    migrated, summary = migrate_payload(
        {
            "version": 1,
            "chunks": [
                {
                    "source_path": source,
                    "content": "Redis timeout",
                    "metadata": {"_chunk_id": "redis.md#0001"},
                    "terms": ["redis", "timeout"],
                }
            ],
            "stale_sources": {source: "failed"},
        }
    )

    assert migrated["chunks"][0]["metadata"]["_source"] == source
    assert migrated["chunks"][0]["metadata"]["_source_id"] == "docs/knowledge-base/redis.md"
    assert migrated["stale_sources"] == {"docs/knowledge-base/redis.md": "failed"}
    assert summary["changed_chunks"] == 1
    assert summary["changed_stale_sources"] == 1


def test_migrate_payload_is_idempotent() -> None:
    source = "docs/knowledge-base/redis.md"
    payload = {
        "version": 1,
        "chunks": [
            {
                "source_path": source,
                "content": "Redis timeout",
                "metadata": {
                    "_source": source,
                    "_source_id": source,
                    "_chunk_id": "redis.md#0001",
                },
                "terms": ["redis", "timeout"],
            }
        ],
        "stale_sources": {},
    }

    migrated, summary = migrate_payload(payload)

    assert migrated == payload
    assert summary["changed_chunks"] == 0
