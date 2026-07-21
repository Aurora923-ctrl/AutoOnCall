"""Build and verify the exact knowledge-base identity stored in Milvus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.core.milvus_client import milvus_manager
from app.services.document_loaders import document_loader_registry
from app.services.document_splitter_service import document_splitter_service
from app.services.lexical_index_service import lexical_index_service
from app.services.vector_index_service import vector_index_service
from app.services.vector_store_manager import build_vector_document_id

DEFAULT_DOCS_DIR = Path(__file__).resolve().parents[2] / "docs" / "knowledge-base"


def expected_index_records(docs_dir: str | Path = DEFAULT_DOCS_DIR) -> list[dict[str, str]]:
    """Load and split the current assets using the production indexing pipeline."""
    root = Path(docs_dir).resolve()
    records: list[dict[str, str]] = []
    supported = document_loader_registry.supported_extensions
    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower().removeprefix(".") not in supported:
            continue
        loader = document_loader_registry.get_loader(path)
        loaded_documents, _report = loader.load(path)
        chunks = document_splitter_service.split_loaded_documents(
            loaded_documents,
            path.as_posix(),
        )
        snapshot = lexical_index_service.snapshot_source_state(path.as_posix())
        vector_index_service._preserve_existing_chunk_ids(chunks, snapshot)
        for index, chunk in enumerate(chunks, 1):
            metadata = dict(chunk.metadata or {})
            records.append(
                {
                    "id": build_vector_document_id(chunk, index),
                    "source_file": str(metadata.get("_file_name") or path.name),
                    "source_id": str(metadata.get("_source_id") or ""),
                    "chunk_id": str(metadata.get("_chunk_id") or ""),
                    "document_hash": str(metadata.get("_document_hash") or ""),
                    "chunk_hash": str(metadata.get("_chunk_hash") or ""),
                }
            )
    return records


def query_index_records(*, batch_size: int = 1000) -> list[dict[str, str]]:
    """Read stable identity fields from the active production collection."""
    _ = milvus_manager.connect()
    iterator = milvus_manager.get_collection().query_iterator(
        expr='id != ""',
        output_fields=["id", "metadata"],
        batch_size=batch_size,
    )
    records = []
    try:
        while True:
            rows = iterator.next()
            if not rows:
                break
            for row in rows:
                metadata = row.get("metadata") if isinstance(row, dict) else {}
                metadata = metadata if isinstance(metadata, dict) else {}
                records.append(
                    {
                        "id": str(row.get("id") or ""),
                        "source_file": str(
                            metadata.get("_file_name") or metadata.get("source_file") or ""
                        ),
                        "source_id": str(metadata.get("_source_id") or ""),
                        "chunk_id": str(
                            metadata.get("_chunk_id") or metadata.get("chunk_id") or ""
                        ),
                        "document_hash": str(metadata.get("_document_hash") or ""),
                        "chunk_hash": str(metadata.get("_chunk_hash") or ""),
                    }
                )
    finally:
        iterator.close()
    return records


def assess_index_identity(
    expected: list[dict[str, str]],
    actual: list[dict[str, str]],
) -> dict[str, Any]:
    """Require a one-to-one match for vector, source, chunk, and content identities."""
    expected_by_id = {item["id"]: item for item in expected if item.get("id")}
    actual_by_id = {item["id"]: item for item in actual if item.get("id")}
    duplicate_expected_ids = len(expected_by_id) != len(expected)
    duplicate_actual_ids = len(actual_by_id) != len(actual)
    missing_ids = sorted(set(expected_by_id) - set(actual_by_id))
    unexpected_ids = sorted(set(actual_by_id) - set(expected_by_id))
    mismatches = []
    for vector_id in sorted(set(expected_by_id) & set(actual_by_id)):
        expected_item = expected_by_id[vector_id]
        actual_item = actual_by_id[vector_id]
        fields = {
            field: {
                "expected": expected_item.get(field, ""),
                "actual": actual_item.get(field, ""),
            }
            for field in (
                "source_file",
                "source_id",
                "chunk_id",
                "document_hash",
                "chunk_hash",
            )
            if expected_item.get(field, "") != actual_item.get(field, "")
        }
        if fields:
            mismatches.append({"id": vector_id, "fields": fields})
    status = (
        "passed"
        if not duplicate_expected_ids
        and not duplicate_actual_ids
        and not missing_ids
        and not unexpected_ids
        and not mismatches
        else "failed"
    )
    return {
        "status": status,
        "expected_chunk_count": len(expected),
        "actual_chunk_count": len(actual),
        "duplicate_expected_ids": duplicate_expected_ids,
        "duplicate_actual_ids": duplicate_actual_ids,
        "missing_ids": missing_ids,
        "unexpected_ids": unexpected_ids,
        "identity_mismatches": mismatches,
        "expected_sources": sorted(
            {str(item.get("source_file") or "") for item in expected if item.get("source_file")}
        ),
        "actual_sources": sorted(
            {str(item.get("source_file") or "") for item in actual if item.get("source_file")}
        ),
    }


def verify_active_index_identity(
    docs_dir: str | Path = DEFAULT_DOCS_DIR,
) -> dict[str, Any]:
    """Compare current assets with both active vector and lexical indexes."""
    expected = expected_index_records(docs_dir)
    vector = assess_index_identity(expected, query_index_records())
    lexical = assess_lexical_identity(expected, lexical_index_service.identity_records())
    return {
        **vector,
        "status": "passed" if vector["status"] == lexical["status"] == "passed" else "failed",
        "vector": vector,
        "lexical": lexical,
    }


def assess_lexical_identity(
    expected: list[dict[str, str]],
    actual: list[dict[str, str]],
) -> dict[str, Any]:
    """Compare lexical chunks without requiring Milvus primary keys."""
    fields = ("source_file", "source_id", "chunk_id", "document_hash", "chunk_hash")
    expected_keys = [tuple(item.get(field, "") for field in fields) for item in expected]
    actual_keys = [tuple(item.get(field, "") for field in fields) for item in actual]
    expected_set = set(expected_keys)
    actual_set = set(actual_keys)
    return {
        "status": (
            "passed"
            if len(expected_set) == len(expected_keys)
            and len(actual_set) == len(actual_keys)
            and expected_set == actual_set
            else "failed"
        ),
        "expected_chunk_count": len(expected_keys),
        "actual_chunk_count": len(actual_keys),
        "duplicate_expected_records": len(expected_set) != len(expected_keys),
        "duplicate_actual_records": len(actual_set) != len(actual_keys),
        "missing_records": [list(item) for item in sorted(expected_set - actual_set)],
        "unexpected_records": [list(item) for item in sorted(actual_set - expected_set)],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", default=str(DEFAULT_DOCS_DIR))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = verify_active_index_identity(args.docs_dir)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
