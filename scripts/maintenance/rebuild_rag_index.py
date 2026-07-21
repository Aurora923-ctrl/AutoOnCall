"""Cleanly rebuild the production RAG indexes and verify exact asset identity."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility

from app.config import config
from app.core.milvus_client import milvus_manager
from app.services.document_loaders import document_loader_registry
from app.services.document_splitter_service import document_splitter_service
from app.services.lexical_index_service import lexical_index_service
from app.services.vector_embedding_service import vector_embedding_service
from app.services.vector_index_service import vector_index_service
from app.services.vector_store_manager import build_vector_document_id, vector_store_manager
from scripts.eval.rag_index_identity import (
    assess_index_identity,
    assess_lexical_identity,
    query_index_records,
)

DEFAULT_DOCS_DIR = REPO_ROOT / "docs" / "knowledge-base"
DEFAULT_REPORT = REPO_ROOT / "logs" / "rag_index_rebuild.json"


def clean_rebuild(docs_dir: str | Path) -> dict[str, object]:
    """Preflight assets, rebuild a shadow collection, then replace active state."""
    root = Path(docs_dir).resolve()
    documents_by_source = load_all_documents(root)
    all_documents = [
        document
        for source_documents in documents_by_source.values()
        for document in source_documents
    ]
    expected = _identity_records_from_documents(all_documents)
    vectors = vector_embedding_service.embed_documents(
        [str(document.page_content or "") for document in all_documents]
    )
    shadow_collection = f"{milvus_manager.COLLECTION_NAME}_rebuild_{uuid4().hex[:12]}"
    backup_collection = (
        f"{milvus_manager.COLLECTION_NAME}_backup_"
        f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    )
    lexical_snapshot = lexical_index_service.snapshot_index_state()
    active_exists = False
    active_switched = False
    milvus_manager.close()
    vector_store_manager.vector_store = None
    connections.connect(
        alias=milvus_manager.CONNECTION_ALIAS,
        host=config.milvus_host,
        port=str(config.milvus_port),
        timeout=config.milvus_timeout / 1000,
    )
    try:
        create_shadow_collection(shadow_collection)
        bulk_upsert_documents(
            all_documents,
            collection_name=shadow_collection,
            vectors=vectors,
        )
        shadow_actual = query_collection_records(shadow_collection)
        shadow_identity = assess_index_identity(expected, shadow_actual)
        if shadow_identity["status"] != "passed":
            raise RuntimeError("Shadow collection identity verification failed")

        active_exists = utility.has_collection(
            milvus_manager.COLLECTION_NAME,
            using=milvus_manager.CONNECTION_ALIAS,
            timeout=config.milvus_timeout / 1000,
        )
        if active_exists:
            utility.rename_collection(
                milvus_manager.COLLECTION_NAME,
                backup_collection,
                using=milvus_manager.CONNECTION_ALIAS,
                timeout=config.milvus_timeout / 1000,
            )
        try:
            utility.rename_collection(
                shadow_collection,
                milvus_manager.COLLECTION_NAME,
                using=milvus_manager.CONNECTION_ALIAS,
                timeout=config.milvus_timeout / 1000,
            )
            active_switched = True
        except Exception:
            if active_exists:
                utility.rename_collection(
                    backup_collection,
                    milvus_manager.COLLECTION_NAME,
                    using=milvus_manager.CONNECTION_ALIAS,
                    timeout=config.milvus_timeout / 1000,
                )
            raise
    finally:
        connections.disconnect(milvus_manager.CONNECTION_ALIAS)

    try:
        lexical_index_service.replace_all_sources(documents_by_source)
        milvus_manager.close()
        vector_store_manager.vector_store = None
        final_expected = expected
        vector_identity = assess_index_identity(final_expected, query_index_records())
        lexical_identity = assess_lexical_identity(
            final_expected,
            lexical_index_service.identity_records(),
        )
        identity = {
            "status": (
                "passed"
                if vector_identity["status"] == lexical_identity["status"] == "passed"
                else "failed"
            ),
            "vector": vector_identity,
            "lexical": lexical_identity,
        }
        if identity["status"] != "passed":
            raise RuntimeError("Final vector/lexical identity verification failed")
    except Exception:
        lexical_index_service.restore_index_state(lexical_snapshot)
        if active_switched:
            restore_active_collection(
                backup_collection=backup_collection if active_exists else "",
            )
        raise
    vector_ids = [str(item["id"]) for item in expected]
    indexing = {
        "success": len(vector_ids) == len(all_documents),
        "total_files": len(documents_by_source),
        "success_count": len(documents_by_source),
        "fail_count": 0,
        "chunk_count": len(all_documents),
        "mode": "clean_bulk_upsert_single_flush",
    }
    status = "passed" if indexing["success"] and identity["status"] == "passed" else "failed"
    return {
        "status": status,
        "docs_dir": str(root),
        "collection": milvus_manager.COLLECTION_NAME,
        "indexing": indexing,
        "index_identity": identity,
        "backup_collection": backup_collection if active_exists else "",
    }


def restore_active_collection(*, backup_collection: str) -> None:
    """Restore the previous active collection after a post-switch failure."""
    milvus_manager.close()
    vector_store_manager.vector_store = None
    connections.connect(
        alias=milvus_manager.CONNECTION_ALIAS,
        host=config.milvus_host,
        port=str(config.milvus_port),
        timeout=config.milvus_timeout / 1000,
    )
    failed_collection = f"{milvus_manager.COLLECTION_NAME}_failed_{uuid4().hex[:12]}"
    try:
        if utility.has_collection(
            milvus_manager.COLLECTION_NAME,
            using=milvus_manager.CONNECTION_ALIAS,
            timeout=config.milvus_timeout / 1000,
        ):
            utility.rename_collection(
                milvus_manager.COLLECTION_NAME,
                failed_collection,
                using=milvus_manager.CONNECTION_ALIAS,
                timeout=config.milvus_timeout / 1000,
            )
        if backup_collection:
            utility.rename_collection(
                backup_collection,
                milvus_manager.COLLECTION_NAME,
                using=milvus_manager.CONNECTION_ALIAS,
                timeout=config.milvus_timeout / 1000,
            )
    finally:
        connections.disconnect(milvus_manager.CONNECTION_ALIAS)


def load_all_documents(root: Path) -> dict[str, list[object]]:
    """Load every supported source before the single vector write begins."""
    documents_by_source: dict[str, list[object]] = {}
    supported = document_loader_registry.supported_extensions
    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower().removeprefix(".") not in supported:
            continue
        loader = document_loader_registry.get_loader(path)
        loaded_documents, _report = loader.load(path)
        documents = document_splitter_service.split_loaded_documents(
            loaded_documents,
            path.resolve().as_posix(),
        )
        snapshot = lexical_index_service.snapshot_source_state(path.resolve().as_posix())
        vector_index_service._preserve_existing_chunk_ids(documents, snapshot)
        if not documents:
            raise RuntimeError(f"Knowledge asset produced no indexable chunks: {path.name}")
        documents_by_source[path.resolve().as_posix()] = documents
    if not documents_by_source:
        raise RuntimeError(f"No supported knowledge assets found: {root}")
    return documents_by_source


def bulk_upsert_documents(
    documents: list[object],
    *,
    collection_name: str | None = None,
    vectors: list[list[float]] | None = None,
) -> list[str]:
    """Perform one maintenance-only vector write without request-path timeouts."""
    ids = [
        build_vector_document_id(document, index)
        for index, document in enumerate(documents, 1)
    ]
    contents = [str(document.page_content or "") for document in documents]
    metadata = []
    for document, vector_id in zip(documents, ids, strict=True):
        document.metadata["_vector_id"] = vector_id
        metadata.append(dict(document.metadata or {}))
    resolved_vectors = vectors or vector_embedding_service.embed_documents(contents)
    collection = (
        Collection(collection_name, using=milvus_manager.CONNECTION_ALIAS)
        if collection_name
        else (_connect_and_get_active_collection())
    )
    collection.upsert(
        [
            ids,
            resolved_vectors,
            contents,
            metadata,
        ],
        timeout=180,
    )
    collection.flush(timeout=180)
    return ids


def _connect_and_get_active_collection() -> Collection:
    _ = milvus_manager.connect()
    return milvus_manager.get_collection()


def create_shadow_collection(collection_name: str) -> Collection:
    fields = [
        FieldSchema(
            name="id",
            dtype=DataType.VARCHAR,
            max_length=milvus_manager.ID_MAX_LENGTH,
            is_primary=True,
        ),
        FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=milvus_manager.vector_dim),
        FieldSchema(
            name="content",
            dtype=DataType.VARCHAR,
            max_length=milvus_manager.CONTENT_MAX_LENGTH,
        ),
        FieldSchema(name="metadata", dtype=DataType.JSON),
    ]
    collection = Collection(
        collection_name,
        schema=CollectionSchema(fields=fields, enable_dynamic_field=False),
        using=milvus_manager.CONNECTION_ALIAS,
        num_shards=milvus_manager.DEFAULT_SHARD_NUMBER,
    )
    collection.create_index(
        field_name="vector",
        index_params={
            "metric_type": milvus_manager.VECTOR_METRIC_TYPE,
            "index_type": milvus_manager.VECTOR_INDEX_TYPE,
            "params": {"nlist": milvus_manager.VECTOR_INDEX_NLIST},
        },
    )
    return collection


def query_collection_records(collection_name: str) -> list[dict[str, str]]:
    collection = Collection(collection_name, using=milvus_manager.CONNECTION_ALIAS)
    iterator = collection.query_iterator(
        expr='id != ""',
        output_fields=["id", "metadata"],
        batch_size=1000,
    )
    records: list[dict[str, str]] = []
    try:
        while True:
            rows = iterator.next()
            if not rows:
                break
            for row in rows:
                metadata = dict(row.get("metadata") or {})
                records.append(
                    {
                        "id": str(row.get("id") or ""),
                        "source_file": str(metadata.get("_file_name") or ""),
                        "source_id": str(metadata.get("_source_id") or ""),
                        "chunk_id": str(metadata.get("_chunk_id") or ""),
                        "document_hash": str(metadata.get("_document_hash") or ""),
                        "chunk_hash": str(metadata.get("_chunk_hash") or ""),
                    }
                )
    finally:
        iterator.close()
    return records


def _identity_records_from_documents(documents: list[object]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for index, document in enumerate(documents, 1):
        metadata = dict(document.metadata or {})
        records.append(
            {
                "id": build_vector_document_id(document, index),
                "source_file": str(metadata.get("_file_name") or ""),
                "source_id": str(metadata.get("_source_id") or ""),
                "chunk_id": str(metadata.get("_chunk_id") or ""),
                "document_hash": str(metadata.get("_document_hash") or ""),
                "chunk_hash": str(metadata.get("_chunk_hash") or ""),
            }
        )
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", default=str(DEFAULT_DOCS_DIR))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument(
        "--confirm-drop",
        action="store_true",
        help="Required acknowledgement that the production RAG collection will be dropped.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_drop:
        raise SystemExit("--confirm-drop is required")
    payload = clean_rebuild(args.docs_dir)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
