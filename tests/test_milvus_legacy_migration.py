"""Tests for the non-destructive legacy Milvus row migration."""

import pytest

from scripts.maintenance.migrate_legacy_milvus_records import (
    apply_migration,
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


def test_plan_migration_does_not_delete_stable_primary_key_with_legacy_metadata() -> None:
    migrated = build_migrated_record(_legacy_record())
    record = _legacy_record(row_id=migrated["id"])

    plan = plan_migration([record])

    assert plan["legacy_ids"] == []
    assert plan["records"][0]["id"] == migrated["id"]
    assert plan["records"][0]["metadata"]["_vector_id"] == migrated["id"]


def test_apply_migration_never_deletes_target_stable_ids() -> None:
    migrated = build_migrated_record(_legacy_record())

    class FakeClient:
        def __init__(self) -> None:
            self.rows = {migrated["id"]: migrated}
            self.deleted_ids: list[str] = []

        def upsert(self, *, collection_name, data) -> None:
            for row in data:
                self.rows[row["id"]] = row

        def flush(self, *, collection_name) -> None:
            return None

        def query(self, *, collection_name, ids, output_fields):
            return [
                {"id": row_id, "metadata": self.rows[row_id]["metadata"]}
                for row_id in ids
                if row_id in self.rows
            ]

        def delete(self, *, collection_name, ids) -> None:
            self.deleted_ids.extend(ids)
            for row_id in ids:
                self.rows.pop(row_id, None)

    client = FakeClient()
    apply_migration(
        client,
        collection="biz",
        plan={
            "records": [migrated],
            "legacy_ids": [migrated["id"]],
        },
        batch_size=10,
    )

    assert client.deleted_ids == []
    assert migrated["id"] in client.rows
