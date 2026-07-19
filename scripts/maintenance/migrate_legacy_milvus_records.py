"""Migrate legacy Milvus rows to the stable AutoOnCall identity contract.

The command is dry-run by default. Use ``--apply`` to upsert stable rows, verify
them, and only then delete superseded legacy primary keys. It never drops or
recreates a collection.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from langchain_core.documents import Document
from pymilvus import MilvusClient

from app.config import config
from app.core.milvus_client import MilvusClientManager
from app.services.document_splitter_service import canonical_source_id
from app.services.vector_store_manager import build_vector_document_id

DEFAULT_COLLECTION = MilvusClientManager.COLLECTION_NAME
DEFAULT_BATCH_SIZE = 100


def build_migrated_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return one row with stable source and vector identities."""
    metadata = dict(record.get("metadata") or {})
    source = str(
        metadata.get("_source") or metadata.get("source") or metadata.get("_doc_id") or ""
    ).strip()
    if not source:
        raise ValueError(f"row {record.get('id')} has no source identity")
    metadata["_source"] = source
    metadata["_source_id"] = canonical_source_id(source)
    document = Document(
        page_content=str(record.get("content") or ""),
        metadata=metadata,
    )
    stable_id = build_vector_document_id(document)
    metadata["_vector_id"] = stable_id
    return {
        "id": stable_id,
        "vector": record["vector"],
        "content": document.page_content,
        "metadata": metadata,
    }


def plan_migration(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a deterministic migration plan and reject identity collisions."""
    migrated_by_id: dict[str, dict[str, Any]] = {}
    legacy_ids: list[str] = []
    unchanged = 0
    for record in records:
        migrated = build_migrated_record(record)
        current_id = str(record.get("id") or "")
        stable_id = str(migrated["id"])
        existing = migrated_by_id.get(stable_id)
        if existing is not None and (
            existing["content"] != migrated["content"]
            or existing["metadata"].get("_chunk_hash") != migrated["metadata"].get("_chunk_hash")
        ):
            raise ValueError(f"stable vector id collision: {stable_id}")
        migrated_by_id[stable_id] = migrated
        if current_id == stable_id and dict(record.get("metadata") or {}) == migrated["metadata"]:
            unchanged += 1
        elif current_id:
            legacy_ids.append(current_id)
    return {
        "records": list(migrated_by_id.values()),
        "legacy_ids": sorted(set(legacy_ids)),
        "source_rows": len(records),
        "target_rows": len(migrated_by_id),
        "unchanged_rows": unchanged,
    }


def fetch_all_records(client: MilvusClient, collection: str) -> list[dict[str, Any]]:
    """Read all rows required for an in-place identity migration."""
    iterator = client.query_iterator(
        collection_name=collection,
        batch_size=DEFAULT_BATCH_SIZE,
        filter="",
        output_fields=["id", "vector", "content", "metadata"],
    )
    records: list[dict[str, Any]] = []
    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            records.extend(batch)
    finally:
        iterator.close()
    return records


def write_backup(records: list[dict[str, Any]], path: Path) -> None:
    """Persist a rollback artifact before any destructive step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


def apply_migration(
    client: MilvusClient,
    *,
    collection: str,
    plan: dict[str, Any],
    batch_size: int,
) -> None:
    """Upsert, verify, then remove superseded primary keys."""
    records = list(plan["records"])
    for start in range(0, len(records), batch_size):
        client.upsert(collection_name=collection, data=records[start : start + batch_size])
    client.flush(collection_name=collection)

    stable_ids = [str(record["id"]) for record in records]
    verified = 0
    for start in range(0, len(stable_ids), batch_size):
        rows = client.query(
            collection_name=collection,
            ids=stable_ids[start : start + batch_size],
            output_fields=["id", "metadata"],
        )
        for row in rows:
            metadata = dict(row.get("metadata") or {})
            if metadata.get("_vector_id") != row.get("id") or not metadata.get("_source_id"):
                raise RuntimeError(f"verification failed for row {row.get('id')}")
        verified += len(rows)
    if verified != len(stable_ids):
        raise RuntimeError(
            f"migration verification count mismatch: expected={len(stable_ids)}, actual={verified}"
        )

    legacy_ids = list(plan["legacy_ids"])
    for start in range(0, len(legacy_ids), batch_size):
        client.delete(
            collection_name=collection,
            ids=legacy_ids[start : start + batch_size],
        )
    client.flush(collection_name=collection)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be positive")
    client = MilvusClient(
        uri=f"http://{config.milvus_host}:{config.milvus_port}",
        timeout=config.milvus_timeout / 1000,
    )
    try:
        records = fetch_all_records(client, args.collection)
        plan = plan_migration(records)
        summary = {
            key: value for key, value in plan.items() if key not in {"records", "legacy_ids"}
        }
        summary["legacy_rows"] = len(plan["legacy_ids"])
        summary["mode"] = "apply" if args.apply else "dry-run"
        if args.apply:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup = Path(args.backup or f"logs/milvus-{args.collection}-backup-{timestamp}.json")
            write_backup(records, backup)
            apply_migration(
                client,
                collection=args.collection,
                plan=plan,
                batch_size=args.batch_size,
            )
            summary["backup"] = str(backup)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
