"""Tests for the non-destructive legacy Milvus row migration."""

import pytest

from scripts.maintenance.migrate_legacy_milvus_records import (
    build_migrated_record,
    plan_migration,
)


def _legacy_record(*, row_id: str = "legacy-uuid", content: str = "Redis timeout"):
    return {
        "id": row_id,
        "vector": [0.6, 0.8],
        "content": content,
        "metadata": {
            "_source": "C:/repo/aiops-docs/redis.md",
            "_chunk_id": "redis.md#0001",
            "_document_hash": "document-hash",
            "_chunk_hash": "chunk-hash",
        },
    }


def test_build_migrated_record_maps_legacy_root_and_sets_stable_vector_id() -> None:
    migrated = build_migrated_record(_legacy_record())

    assert migrated["id"].startswith("vec-")
    assert migrated["metadata"]["_source_id"] == "docs/knowledge-base/redis.md"
    assert migrated["metadata"]["_vector_id"] == migrated["id"]


def test_plan_migration_deduplicates_identical_legacy_rows() -> None:
    records = [_legacy_record(row_id="legacy-a"), _legacy_record(row_id="legacy-b")]

    plan = plan_migration(records)

    assert plan["source_rows"] == 2
    assert plan["target_rows"] == 1
    assert plan["legacy_ids"] == ["legacy-a", "legacy-b"]


def test_plan_migration_rejects_conflicting_stable_id_collision() -> None:
    records = [
        _legacy_record(row_id="legacy-a", content="Redis timeout"),
        _legacy_record(row_id="legacy-b", content="different content"),
    ]

    with pytest.raises(ValueError, match="collision"):
        plan_migration(records)
