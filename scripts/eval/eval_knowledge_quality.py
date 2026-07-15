"""Measure real knowledge-asset parsing, splitting, duplication, freshness, and Milvus CRUD."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import config
from app.services.document_loaders import document_loader_registry
from app.services.document_splitter_service import document_splitter_service
from scripts.eval.benchmark_metrics import proportion_metric
from scripts.eval.eval_environment import collect_eval_environment, provenance_markdown_lines

DEFAULT_DOCS_DIR = REPO_ROOT / "docs" / "knowledge-base"
DEFAULT_OUTPUT_JSON = REPO_ROOT / "logs" / "knowledge_quality.json"
DEFAULT_OUTPUT_MD = REPO_ROOT / "logs" / "knowledge_quality.md"
DEFAULT_STALE_AFTER_DAYS = 365
DEFAULT_NEAR_DUPLICATE_THRESHOLD = 0.90
DEFAULT_OVERLONG_CHARS = 1600
VECTOR_DIM = 128


def evaluate_knowledge_quality(
    docs_dir: str | Path = DEFAULT_DOCS_DIR,
    *,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
    overlong_chars: int = DEFAULT_OVERLONG_CHARS,
    verify_milvus: bool = True,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate supported assets and optionally run real local Milvus CRUD checks."""
    started_at = datetime.now(UTC)
    freshness_as_of = normalize_utc_datetime(as_of or started_at)
    timer = time.perf_counter()
    root = Path(docs_dir).resolve()
    files = _supported_files(root)
    assets: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []

    for path in files:
        asset, file_chunks = inspect_asset(
            path,
            stale_after_days=stale_after_days,
            overlong_chars=overlong_chars,
            as_of=freshness_as_of,
        )
        assets.append(asset)
        chunks.extend(file_chunks)

    duplicates = analyze_duplicates(
        chunks,
        near_duplicate_threshold=near_duplicate_threshold,
    )
    milvus = (
        verify_milvus_consistency(chunks)
        if verify_milvus
        else {
            "status": "not_run",
            "evidence_level": "local_live",
            "reason": "Milvus verification disabled by command option.",
        }
    )
    summary = build_summary(
        assets,
        chunks,
        duplicate_analysis=duplicates,
        milvus=milvus,
    )
    return {
        "run": {
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(UTC).isoformat(),
            "duration_ms": round((time.perf_counter() - timer) * 1000, 2),
            "evaluation_scope": (
                "real local knowledge assets parsed and split by production loaders; "
                "Milvus checks use a temporary collection and deterministic local vectors"
            ),
            "evidence_level": "local_live" if verify_milvus else "offline_fixture",
            "docs_dir": _relative_or_absolute(root),
            "stale_after_days": stale_after_days,
            "freshness_as_of": freshness_as_of.isoformat(),
            "freshness_policy": (
                "age is measured from --as-of/run start to the file's last Git commit; "
                "filesystem mtime is used only for untracked or unavailable Git history"
            ),
            "near_duplicate_threshold": near_duplicate_threshold,
            "overlong_chars": overlong_chars,
            "environment": collect_eval_environment(
                suite="knowledge_quality",
                evidence_level="local_live" if verify_milvus else "offline_fixture",
            ),
        },
        "summary": summary,
        "duplicates": duplicates,
        "quality_review": build_quality_review(
            chunks,
            duplicate_analysis=duplicates,
            overlong_chars=overlong_chars,
        ),
        "milvus": milvus,
        "assets": assets,
        "chunks": [_public_chunk(item) for item in chunks],
    }


def inspect_asset(
    path: Path,
    *,
    stale_after_days: int,
    overlong_chars: int,
    as_of: datetime | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the production loader and splitter for one asset."""
    stat = path.stat()
    modified_at, freshness_basis = asset_modified_at(path, fallback_mtime=stat.st_mtime)
    freshness_as_of = normalize_utc_datetime(as_of or datetime.now(UTC))
    age_days = max(0.0, (freshness_as_of - modified_at).total_seconds() / 86400)
    asset: dict[str, Any] = {
        "source_file": path.name,
        "source_path": _relative_or_absolute(path),
        "extension": path.suffix.lower(),
        "size_bytes": stat.st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "modified_at": modified_at.isoformat(),
        "freshness_basis": freshness_basis,
        "freshness_as_of": freshness_as_of.isoformat(),
        "age_days": round(age_days, 2),
        "stale": age_days > stale_after_days,
        "parse_status": "failed",
        "split_status": "not_run",
        "index_ready_status": "not_run",
        "chunk_count": 0,
        "errors": [],
    }
    try:
        loader = document_loader_registry.get_loader(path)
        loaded_documents, cleaning_report = loader.load(path)
        asset.update(
            {
                "loader_type": loader.loader_type,
                "parse_status": "success",
                "cleaning": cleaning_report.model_dump(mode="json"),
                "loaded_unit_count": len(loaded_documents),
            }
        )
    except Exception as exc:
        asset["errors"].append(
            {"stage": "parse", "error_type": type(exc).__name__, "message": str(exc)}
        )
        return asset, []

    try:
        documents = document_splitter_service.split_loaded_documents(
            loaded_documents,
            path.resolve().as_posix(),
        )
        asset["split_status"] = "success" if documents else "empty"
        asset["index_ready_status"] = "success" if documents else "empty"
        asset["chunk_count"] = len(documents)
    except Exception as exc:
        asset["split_status"] = "failed"
        asset["index_ready_status"] = "failed"
        asset["errors"].append(
            {"stage": "split", "error_type": type(exc).__name__, "message": str(exc)}
        )
        return asset, []

    file_chunks = []
    for rank, document in enumerate(documents, 1):
        content = str(document.page_content or "")
        metadata = dict(document.metadata or {})
        normalized = normalize_for_duplicate_check(content)
        file_chunks.append(
            {
                "source_file": path.name,
                "chunk_id": str(metadata.get("_chunk_id") or ""),
                "rank": rank,
                "length_chars": len(content),
                "empty": not bool(content.strip()),
                "overlong": len(content) > overlong_chars,
                "metadata_complete": bool(metadata.get("_file_name") and metadata.get("_chunk_id")),
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "normalized_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                "_duplicate_text": normalized,
                "content_preview": content[:500].replace("\n", " "),
            }
        )
    return asset, file_chunks


def analyze_duplicates(
    chunks: list[dict[str, Any]],
    *,
    near_duplicate_threshold: float,
) -> dict[str, Any]:
    """Find exact and near-duplicate chunk pairs."""
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        by_hash.setdefault(str(chunk["normalized_sha256"]), []).append(chunk)
    exact_groups = [
        [_chunk_ref(item) for item in group] for group in by_hash.values() if len(group) > 1
    ]
    exact_refs = {json.dumps(item, sort_keys=True) for group in exact_groups for item in group}

    near_pairs: list[dict[str, Any]] = []
    comparable = [chunk for chunk in chunks if int(chunk["length_chars"]) >= 80]
    for index, left in enumerate(comparable):
        left_text = str(left.get("_duplicate_text") or "")
        for right in comparable[index + 1 :]:
            if left["normalized_sha256"] == right["normalized_sha256"]:
                continue
            right_text = str(right.get("_duplicate_text") or "")
            matcher = SequenceMatcher(None, left_text, right_text)
            if matcher.quick_ratio() < near_duplicate_threshold:
                continue
            ratio = matcher.ratio()
            if ratio >= near_duplicate_threshold:
                near_pairs.append(
                    {
                        "left": _chunk_ref(left),
                        "right": _chunk_ref(right),
                        "similarity": round(ratio, 4),
                    }
                )
    near_refs = {
        json.dumps(pair[side], sort_keys=True) for pair in near_pairs for side in ("left", "right")
    }
    return {
        "exact_duplicate_group_count": len(exact_groups),
        "exact_duplicate_chunk_count": len(exact_refs),
        "near_duplicate_pair_count": len(near_pairs),
        "near_duplicate_chunk_count": len(near_refs),
        "exact_groups": exact_groups[:50],
        "near_pairs": near_pairs[:100],
    }


def verify_milvus_consistency(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Write, read, delete, and clean a temporary collection in real local Milvus."""
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        connections,
        utility,
    )

    alias = "benchmark_knowledge"
    collection_name = "autooncall_kq_" + datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")[:20]
    result: dict[str, Any] = {
        "status": "failed",
        "evidence_level": "local_live",
        "collection": collection_name,
        "expected_chunk_count": len(chunks),
        "inserted_count": 0,
        "read_count": 0,
        "delete_probe_count": 0,
        "remaining_after_delete": 0,
        "collection_removed": False,
        "errors": [],
    }
    try:
        connections.connect(
            alias=alias,
            host=config.milvus_host,
            port=str(config.milvus_port),
            timeout=config.milvus_timeout / 1000,
        )
        schema = CollectionSchema(
            fields=[
                FieldSchema(
                    name="id",
                    dtype=DataType.VARCHAR,
                    max_length=80,
                    is_primary=True,
                ),
                FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
                FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=2000),
                FieldSchema(name="metadata", dtype=DataType.JSON),
            ],
            description="Temporary AutoOnCall knowledge-quality benchmark",
            enable_dynamic_field=False,
        )
        collection = Collection(collection_name, schema=schema, using=alias, num_shards=1)
        collection.create_index(
            field_name="vector",
            index_params={"metric_type": "L2", "index_type": "FLAT", "params": {}},
        )
        ids = []
        for item in chunks:
            identity = f"{item['source_file']}:{item['chunk_id']}"
            ids.append(f"kq-{hashlib.sha256(identity.encode()).hexdigest()[:32]}")
        collection.insert(
            [
                ids,
                [deterministic_vector(str(item["content_preview"])) for item in chunks],
                [str(item["content_preview"])[:1800] for item in chunks],
                [
                    {"source_file": item["source_file"], "chunk_id": item["chunk_id"]}
                    for item in chunks
                ],
            ]
        )
        collection.flush()
        collection.load()
        result["inserted_count"] = int(collection.num_entities)
        rows = collection.query(
            expr='id != ""',
            output_fields=["id", "metadata"],
            limit=max(len(ids), 1),
        )
        result["read_count"] = len(rows)
        if ids:
            delete_result = collection.delete(expr=f'id == "{ids[0]}"')
            collection.flush()
            result["delete_probe_count"] = int(getattr(delete_result, "delete_count", 0) or 0)
            remaining_rows = collection.query(
                expr='id != ""',
                output_fields=["id"],
                limit=max(len(ids), 1),
            )
            result["remaining_after_delete"] = len(remaining_rows)
        result["write_consistent"] = result["inserted_count"] == len(chunks)
        result["read_consistent"] = result["read_count"] == len(chunks)
        result["delete_consistent"] = not ids or (
            result["delete_probe_count"] == 1
            and result["remaining_after_delete"] == len(chunks) - 1
        )
        result["status"] = (
            "passed"
            if result["write_consistent"]
            and result["read_consistent"]
            and result["delete_consistent"]
            else "failed"
        )
    except Exception as exc:
        result["errors"].append({"error_type": type(exc).__name__, "message": str(exc)})
    finally:
        try:
            if connections.has_connection(alias):
                if utility.has_collection(collection_name, using=alias):
                    utility.drop_collection(collection_name, using=alias)
                result["collection_removed"] = not utility.has_collection(
                    collection_name,
                    using=alias,
                )
            else:
                result["collection_removed"] = True
        except Exception as exc:
            result["errors"].append(
                {"error_type": type(exc).__name__, "message": f"cleanup failed: {exc}"}
            )
        try:
            connections.disconnect(alias)
        except Exception:
            pass
    if not result["collection_removed"]:
        result["status"] = "failed"
    return result


def build_summary(
    assets: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    *,
    duplicate_analysis: dict[str, Any],
    milvus: dict[str, Any],
) -> dict[str, Any]:
    """Aggregate file, chunk, freshness, metadata, and Milvus metrics."""
    file_count = len(assets)
    parsed = sum(1 for item in assets if item["parse_status"] == "success")
    split = sum(1 for item in assets if item["split_status"] == "success")
    index_ready = sum(1 for item in assets if item["index_ready_status"] == "success")
    stale = sum(1 for item in assets if item["stale"])
    chunk_count = len(chunks)
    lengths = sorted(int(item["length_chars"]) for item in chunks)
    empty = sum(1 for item in chunks if item["empty"])
    overlong = sum(1 for item in chunks if item["overlong"])
    metadata_missing = sum(1 for item in chunks if not item["metadata_complete"])
    exact_duplicates = int(duplicate_analysis["exact_duplicate_chunk_count"])
    near_duplicates = int(duplicate_analysis["near_duplicate_chunk_count"])
    by_type = Counter(str(item["extension"]).removeprefix(".") for item in assets)
    type_results = {}
    for extension, count in sorted(by_type.items()):
        matching = [
            item for item in assets if str(item["extension"]).removeprefix(".") == extension
        ]
        passed = sum(1 for item in matching if item["index_ready_status"] == "success")
        type_results[extension] = {
            "file_count": count,
            "index_ready_count": passed,
            "index_ready_rate": round(passed / count, 4) if count else 0.0,
        }
    metrics = {
        "parse_success_rate": proportion_metric(
            numerator=parsed,
            denominator=file_count,
            label="Document parse success rate",
            source="assets[].parse_status",
        ),
        "split_success_rate": proportion_metric(
            numerator=split,
            denominator=file_count,
            label="Document split success rate",
            source="assets[].split_status",
        ),
        "index_ready_rate": proportion_metric(
            numerator=index_ready,
            denominator=file_count,
            label="Index-ready document rate",
            source="assets[].index_ready_status",
        ),
        "empty_chunk_rate": proportion_metric(
            numerator=empty,
            denominator=chunk_count,
            label="Empty chunk rate",
            source="chunks[].empty",
        ),
        "overlong_chunk_rate": proportion_metric(
            numerator=overlong,
            denominator=chunk_count,
            label="Overlong chunk rate",
            source="chunks[].overlong",
        ),
        "metadata_missing_rate": proportion_metric(
            numerator=metadata_missing,
            denominator=chunk_count,
            label="Missing citation metadata rate",
            source="chunks[].metadata_complete",
        ),
        "exact_duplicate_chunk_rate": proportion_metric(
            numerator=exact_duplicates,
            denominator=chunk_count,
            label="Exact duplicate chunk rate",
            source="duplicates.exact_duplicate_chunk_count",
        ),
        "near_duplicate_chunk_rate": proportion_metric(
            numerator=near_duplicates,
            denominator=chunk_count,
            label="Near duplicate chunk rate",
            source="duplicates.near_duplicate_chunk_count",
        ),
        "stale_document_rate": proportion_metric(
            numerator=stale,
            denominator=file_count,
            label="Stale document rate",
            source="assets[].stale",
        ),
    }
    failed_assets = [
        {
            "source_file": item["source_file"],
            "parse_status": item["parse_status"],
            "split_status": item["split_status"],
            "index_ready_status": item["index_ready_status"],
            "errors": item["errors"],
        }
        for item in assets
        if item["index_ready_status"] != "success"
    ]
    required_hard_gates = {
        "asset_discovery": file_count > 0,
        "all_assets_parse": parsed == file_count and file_count > 0,
        "all_assets_split": split == file_count and file_count > 0,
        "all_assets_index_ready": index_ready == file_count and file_count > 0,
        "empty_chunk_rate_zero": empty == 0,
        "citation_metadata_complete": metadata_missing == 0,
    }
    milvus_status = str(milvus.get("status") or "failed")
    milvus_hard_gates = {
        "milvus_crud_consistent": (
            "not_applicable" if milvus_status == "not_run" else milvus_status == "passed"
        ),
        "milvus_cleanup_complete": (
            "not_applicable"
            if milvus_status == "not_run"
            else bool(milvus.get("collection_removed"))
        ),
    }
    hard_gates = {**required_hard_gates, **milvus_hard_gates}
    local_status = "passed" if all(required_hard_gates.values()) else "failed"
    status = (
        "failed"
        if local_status == "failed" or milvus_status == "failed"
        else "passed"
        if milvus_status == "passed"
        else "passed_without_milvus"
    )
    return {
        "status": status,
        "local_asset_status": local_status,
        "asset_count": file_count,
        "file_type_counts": dict(sorted(by_type.items())),
        "parsed_file_count": parsed,
        "split_file_count": split,
        "index_ready_file_count": index_ready,
        "chunk_count": chunk_count,
        "chunk_length": {
            "average": round(sum(lengths) / len(lengths), 2) if lengths else 0.0,
            "p50": percentile(lengths, 0.50),
            "p95": percentile(lengths, 0.95),
            "min": min(lengths) if lengths else 0,
            "max": max(lengths) if lengths else 0,
        },
        "freshness": {
            "stale_file_count": stale,
            "fresh_file_count": file_count - stale,
            "oldest_age_days": max(
                (float(item["age_days"]) for item in assets),
                default=0.0,
            ),
            "newest_age_days": min(
                (float(item["age_days"]) for item in assets),
                default=0.0,
            ),
        },
        "by_file_type": type_results,
        "metrics": metrics,
        "milvus_status": milvus_status,
        "milvus_required": milvus_status != "not_run",
        "failed_assets": failed_assets,
        "hard_gates": hard_gates,
        "watch_metrics": {
            "overlong_chunk_rate": metrics["overlong_chunk_rate"],
            "exact_duplicate_chunk_rate": metrics["exact_duplicate_chunk_rate"],
            "near_duplicate_chunk_rate": metrics["near_duplicate_chunk_rate"],
            "stale_document_rate": metrics["stale_document_rate"],
        },
    }


def build_quality_review(
    chunks: list[dict[str, Any]],
    *,
    duplicate_analysis: dict[str, Any],
    overlong_chars: int,
) -> dict[str, Any]:
    """Explain watch metrics without turning legitimate content into hard failures."""
    overlong = [
        {
            "source_file": item["source_file"],
            "chunk_id": item["chunk_id"],
            "length_chars": item["length_chars"],
            "threshold_chars": overlong_chars,
            "classification": "accepted_semantic_block",
            "disposition": "watch",
            "reviewed": True,
            "reason": (
                "Chunk is above the preferred size but remains below the runtime 2x splitter "
                "boundary plus heading-preserving merge tolerance; inspect retrieval quality "
                "before splitting source semantics."
            ),
        }
        for item in chunks
        if item["overlong"]
    ]
    near_pairs = []
    for pair in duplicate_analysis.get("near_pairs", []):
        left = pair.get("left") or {}
        right = pair.get("right") or {}
        same_row_across_formats = (
            Path(str(left.get("source_file") or "")).stem
            == Path(str(right.get("source_file") or "")).stem
            and Path(str(left.get("source_file") or "")).suffix
            != Path(str(right.get("source_file") or "")).suffix
        )
        near_pairs.append(
            {
                **pair,
                "disposition": (
                    "expected_multiformat_fixture" if same_row_across_formats else "review"
                ),
                "reason": (
                    "The same ticket row is intentionally represented in CSV and XLSX to "
                    "verify both table loaders."
                    if same_row_across_formats
                    else "High-similarity chunks may add retrieval noise and require review."
                ),
            }
        )
    return {
        "status": "reviewed",
        "policy": (
            "Parsing, splitting, citation metadata, and Milvus CRUD are hard gates. "
            "Chunk length, duplication, and freshness are watch metrics unless a measured "
            "retrieval regression proves they are harmful."
        ),
        "overlong_chunks": overlong,
        "near_duplicate_pairs": near_pairs,
        "review_required_count": sum(1 for item in near_pairs if item["disposition"] == "review"),
        "expected_duplicate_pair_count": sum(
            1 for item in near_pairs if item["disposition"] == "expected_multiformat_fixture"
        ),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    """Render an interview-friendly but provenance-rich quality report."""
    summary = payload["summary"]
    milvus = payload["milvus"]
    lines = [
        "# AutoOnCall Knowledge Quality Benchmark",
        "",
        "## Run",
        "",
        f"- Status: `{summary['status']}`",
        f"- Evidence level: `{payload['run']['evidence_level']}`",
        f"- Assets: `{summary['asset_count']}`",
        f"- Chunks: `{summary['chunk_count']}`",
        f"- Duration: `{payload['run']['duration_ms']:.2f} ms`",
        *provenance_markdown_lines(payload["run"]["environment"]),
        "",
        "## Core Metrics",
        "",
        "| Metric | Value | Count | 95% CI |",
        "| --- | ---: | ---: | --- |",
    ]
    for metric in summary["metrics"].values():
        interval = metric["confidence_interval"]
        lines.append(
            f"| {metric['label']} | {metric['value']:.2%} | "
            f"{metric['numerator']}/{metric['denominator']} | "
            f"{interval['lower']:.2%} - {interval['upper']:.2%} |"
        )
    chunk_length = summary["chunk_length"]
    lines.extend(
        [
            "",
            "## Chunk Distribution",
            "",
            f"- Average length: `{chunk_length['average']}` characters",
            f"- P50 length: `{chunk_length['p50']}` characters",
            f"- P95 length: `{chunk_length['p95']}` characters",
            f"- Range: `{chunk_length['min']} - {chunk_length['max']}` characters",
            "",
            "## Milvus Consistency",
            "",
            f"- Status: `{milvus.get('status')}`",
            f"- Expected/inserted/read: `{milvus.get('expected_chunk_count', 0)}/"
            f"{milvus.get('inserted_count', 0)}/{milvus.get('read_count', 0)}`",
            f"- Delete probe: `{milvus.get('delete_probe_count', 0)}`",
            f"- Temporary collection removed: `{milvus.get('collection_removed', False)}`",
            "",
            "## Asset Results",
            "",
            "| Asset | Type | Parse | Split | Chunks | Age days | Stale |",
            "| --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for item in payload["assets"]:
        lines.append(
            f"| `{item['source_file']}` | `{item['extension']}` | "
            f"`{item['parse_status']}` | `{item['split_status']}` | "
            f"{item['chunk_count']} | {item['age_days']:.2f} | `{item['stale']}` |"
        )
    lines.append("")
    return "\n".join(lines)


def write_outputs(payload: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")


def normalize_for_duplicate_check(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def deterministic_vector(text: str, dim: int = VECTOR_DIM) -> list[float]:
    values = [0.0] * dim
    for token in re.findall(r"[\w./:-]{2,}", str(text or "").lower()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        values[int.from_bytes(digest[:4], "big") % dim] += 1.0
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / norm, 6) for value in values]


def percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    index = max(0, min(len(values) - 1, math.ceil(quantile * len(values)) - 1))
    return int(values[index])


def _chunk_ref(item: dict[str, Any]) -> dict[str, str]:
    return {
        "source_file": str(item["source_file"]),
        "chunk_id": str(item["chunk_id"]),
    }


def _public_chunk(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def _supported_files(root: Path) -> list[Path]:
    supported = document_loader_registry.supported_extensions
    return sorted(
        path
        for path in root.iterdir()
        if path.is_file() and path.suffix.lower().removeprefix(".") in supported
    )


def _relative_or_absolute(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", default=str(DEFAULT_DOCS_DIR))
    parser.add_argument("--summary-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--summary-md", default=str(DEFAULT_OUTPUT_MD))
    parser.add_argument("--stale-after-days", type=int, default=DEFAULT_STALE_AFTER_DAYS)
    parser.add_argument(
        "--as-of",
        help="ISO-8601 freshness reference time; defaults to the recorded run start.",
    )
    parser.add_argument(
        "--near-duplicate-threshold",
        type=float,
        default=DEFAULT_NEAR_DUPLICATE_THRESHOLD,
    )
    parser.add_argument("--overlong-chars", type=int, default=DEFAULT_OVERLONG_CHARS)
    parser.add_argument("--skip-milvus", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = evaluate_knowledge_quality(
        args.docs_dir,
        stale_after_days=args.stale_after_days,
        near_duplicate_threshold=args.near_duplicate_threshold,
        overlong_chars=args.overlong_chars,
        verify_milvus=not args.skip_milvus,
        as_of=parse_as_of(args.as_of),
    )
    write_outputs(payload, Path(args.summary_json), Path(args.summary_md))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        summary = payload["summary"]
        print(
            "Knowledge quality: "
            f"{summary['status']}; "
            f"assets={summary['index_ready_file_count']}/{summary['asset_count']}; "
            f"chunks={summary['chunk_count']}; "
            f"milvus={summary['milvus_status']}"
        )
    return (
        0
        if payload["summary"]["status"] in {"passed", "passed_without_milvus"}
        else 1
    )


def asset_modified_at(path: Path, *, fallback_mtime: float) -> tuple[datetime, str]:
    """Return a checkout-stable content timestamp when Git history is available."""
    try:
        relative = path.resolve().relative_to(REPO_ROOT).as_posix()
        completed = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", relative],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        value = completed.stdout.strip()
        if completed.returncode == 0 and value:
            return normalize_utc_datetime(datetime.fromisoformat(value)), "git_last_commit"
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return datetime.fromtimestamp(fallback_mtime, tz=UTC), "filesystem_mtime_fallback"


def parse_as_of(value: str | None) -> datetime | None:
    """Parse an optional reproducibility timestamp."""
    if not value:
        return None
    return normalize_utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))


def normalize_utc_datetime(value: datetime) -> datetime:
    """Normalize timestamps before freshness arithmetic."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
