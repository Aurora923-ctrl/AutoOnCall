"""Verify that interview multi-source RAG assets are actually written to Milvus.

This script intentionally uses deterministic local vectors instead of cloud embeddings.
It is a storage/provenance proof for PDF/HTML/CSV/XLSX chunks entering Milvus, not a
semantic-quality benchmark. Retrieval quality remains covered by eval_rag_cases.py.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility

from app.config import config
from app.services.document_loaders import document_loader_registry
from app.services.document_splitter_service import document_splitter_service
from scripts.eval.eval_environment import collect_eval_environment, provenance_markdown_lines

DEFAULT_DOCS_DIR = REPO_ROOT / "docs" / "knowledge-base"
DEFAULT_OUTPUT_JSON = REPO_ROOT / "logs" / "milvus_multisource_verification.json"
DEFAULT_OUTPUT_MD = REPO_ROOT / "logs" / "milvus_multisource_verification.md"
COLLECTION_NAME = "autooncall_interview_multisource"
VECTOR_DIM = 256

REQUIRED_FILES = [
    "redis_postmortem.pdf",
    "mysql_slow_query_postmortem.pdf",
    "redis_capacity_wiki.html",
    "payment_wiki.html",
    "tickets.csv",
    "tickets.xlsx",
]

PROBES = [
    {
        "id": "redis_pdf_postmortem",
        "query": "connected_clients 9940 maxclients 10000 blocked_clients 37",
        "expected_source": "redis_postmortem.pdf",
    },
    {
        "id": "mysql_pdf_postmortem",
        "query": "slow_queries 18 active_connections 188 pool_waiting 6",
        "expected_source": "mysql_slow_query_postmortem.pdf",
    },
    {
        "id": "redis_html_wiki",
        "query": "Redis Capacity Wiki incident-window live_info approval boundary",
        "expected_source": "redis_capacity_wiki.html",
    },
    {
        "id": "payment_html_wiki",
        "query": "Payment Runbook MySQL slow query EXPLAIN pool_waiting",
        "expected_source": "payment_wiki.html",
    },
    {
        "id": "ticket_csv_history",
        "query": "INC-REDIS-009 promotion lookup retry loop maxclients",
        "expected_source": "tickets.csv",
    },
    {
        "id": "deploy_xlsx_history",
        "query": "payment-api-2026.07.06-rc4 checkout report feature flag index change",
        "expected_source": "tickets.xlsx",
    },
]


def load_documents(docs_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for file_name in REQUIRED_FILES:
        path = docs_dir / file_name
        loader = document_loader_registry.get_loader(path)
        loaded_documents, _report = loader.load(path)
        chunks = document_splitter_service.split_loaded_documents(
            loaded_documents,
            path.resolve().as_posix(),
        )
        for chunk in chunks:
            metadata = dict(chunk.metadata or {})
            metadata["source_file"] = file_name
            metadata["loader_type"] = loader.loader_type
            metadata["verification_scope"] = "interview_multisource_milvus"
            records.append(
                {
                    "id": build_record_id(file_name, metadata.get("_chunk_id"), chunk.page_content),
                    "content": str(chunk.page_content or "")[:7900],
                    "metadata": metadata,
                    "vector": deterministic_vector(
                        " ".join(
                            [
                                file_name,
                                str(metadata.get("heading_path") or ""),
                                str(metadata.get("primary_key") or ""),
                                str(chunk.page_content or ""),
                            ]
                        )
                    ),
                }
            )
    return records


def build_record_id(file_name: str, chunk_id: Any, content: str) -> str:
    digest = hashlib.sha256(f"{file_name}\n{chunk_id}\n{content}".encode()).hexdigest()
    return f"ims-{digest[:32]}"


def deterministic_vector(text: str, dim: int = VECTOR_DIM) -> list[float]:
    values = [0.0] * dim
    for token in extract_terms(text):
        digest = hashlib.sha256(token.encode()).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        values[bucket] += 1.0
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / norm, 6) for value in values]


def extract_terms(text: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9][a-z0-9_.:/-]{1,}", str(text or "").lower())


def connect_milvus() -> None:
    connections.connect(
        alias="default",
        host=config.milvus_host,
        port=str(config.milvus_port),
        timeout=config.milvus_timeout / 1000,
    )


def recreate_collection() -> Collection:
    if utility.has_collection(COLLECTION_NAME):
        utility.drop_collection(COLLECTION_NAME)
    schema = CollectionSchema(
        fields=[
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=80, is_primary=True),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=8000),
            FieldSchema(name="metadata", dtype=DataType.JSON),
        ],
        description="Interview multi-source RAG Milvus verification collection",
        enable_dynamic_field=False,
    )
    collection = Collection(COLLECTION_NAME, schema=schema, num_shards=1)
    collection.create_index(
        field_name="vector",
        index_params={"metric_type": "L2", "index_type": "FLAT", "params": {}},
    )
    return collection


def insert_records(collection: Collection, records: list[dict[str, Any]]) -> None:
    collection.insert(
        [
            [record["id"] for record in records],
            [record["vector"] for record in records],
            [record["content"] for record in records],
            [record["metadata"] for record in records],
        ]
    )
    collection.flush()
    collection.load()


def run_probes(collection: Collection) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for probe in PROBES:
        hits = collection.search(
            data=[deterministic_vector(str(probe["query"]))],
            anns_field="vector",
            param={"metric_type": "L2", "params": {}},
            limit=5,
            output_fields=["content", "metadata"],
        )
        retrieved = []
        for hit in hits[0]:
            entity = hit.entity
            metadata = dict(entity.get("metadata") or {})
            retrieved.append(
                {
                    "source_file": metadata.get("source_file"),
                    "chunk_id": metadata.get("_chunk_id"),
                    "loader_type": metadata.get("loader_type"),
                    "score": float(hit.distance),
                }
            )
        expected_source = probe["expected_source"]
        results.append(
            {
                **probe,
                "passed": any(item.get("source_file") == expected_source for item in retrieved),
                "retrieved": retrieved,
            }
        )
    return results


def summarize(records: list[dict[str, Any]], probes: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = defaultdict(int)
    by_source: dict[str, int] = defaultdict(int)
    for record in records:
        metadata = dict(record["metadata"])
        by_type[str(metadata.get("doc_type") or metadata.get("loader_type") or "unknown")] += 1
        by_source[str(metadata.get("source_file") or "unknown")] += 1
    passed = sum(1 for probe in probes if probe["passed"])
    return {
        "run": {
            "generated_at": datetime.now(UTC).isoformat(),
            "collection": COLLECTION_NAME,
            "vector_dim": VECTOR_DIM,
            "scope": (
                "Milvus storage/provenance verification for multi-source RAG assets; "
                "deterministic local vectors are used to avoid cloud embedding dependency."
            ),
            "environment": collect_eval_environment(suite="milvus_multisource"),
        },
        "summary": {
            "status": "passed" if passed == len(probes) else "failed",
            "inserted_chunks": len(records),
            "doc_type_counts": dict(sorted(by_type.items())),
            "source_counts": dict(sorted(by_source.items())),
            "probe_count": len(probes),
            "passed_probe_count": passed,
            "pass_rate": passed / len(probes) if probes else 0.0,
        },
        "probes": probes,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Milvus Multi-Source RAG Verification",
        "",
        "## Summary",
        "",
        f"- Status: `{summary['status']}`",
        f"- Collection: `{payload['run']['collection']}`",
        f"- Inserted chunks: `{summary['inserted_chunks']}`",
        f"- Probe pass rate: `{summary['passed_probe_count']}/{summary['probe_count']}`",
        f"- Scope: {payload['run']['scope']}",
        *provenance_markdown_lines(payload["run"]["environment"]),
        "",
        "## Source Coverage",
        "",
        "| Source | Chunks |",
        "| --- | ---: |",
    ]
    for source, count in summary["source_counts"].items():
        lines.append(f"| `{source}` | {count} |")
    lines.extend(
        [
            "",
            "## Probe Results",
            "",
            "| Probe | Expected source | Status | Top sources |",
            "| --- | --- | --- | --- |",
        ]
    )
    for probe in payload["probes"]:
        top_sources = ", ".join(
            str(item.get("source_file")) for item in probe.get("retrieved", [])[:3]
        )
        lines.append(
            f"| `{probe['id']}` | `{probe['expected_source']}` | "
            f"`{'PASS' if probe['passed'] else 'FAIL'}` | {top_sources} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_outputs(payload: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", default=str(DEFAULT_DOCS_DIR))
    parser.add_argument("--summary-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--summary-md", default=str(DEFAULT_OUTPUT_MD))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    records = load_documents(Path(args.docs_dir))
    connect_milvus()
    try:
        collection = recreate_collection()
        insert_records(collection, records)
        probes = run_probes(collection)
        payload = summarize(records, probes)
    finally:
        connections.disconnect("default")
    write_outputs(payload, Path(args.summary_json), Path(args.summary_md))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            "Milvus multi-source verification: "
            f"{payload['summary']['status']}; "
            f"chunks={payload['summary']['inserted_chunks']}; "
            f"probes={payload['summary']['passed_probe_count']}/{payload['summary']['probe_count']}"
        )
    return 0 if payload["summary"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
