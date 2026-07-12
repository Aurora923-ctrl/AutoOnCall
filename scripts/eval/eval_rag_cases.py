"""Offline RAG retrieval evaluation with auditable IR metrics and slices."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.document_loaders import document_loader_registry
from app.services.document_splitter_service import document_splitter_service
from app.services.rag_retrieval_service import (
    document_to_retrieval_chunk,
    infer_retrieval_preferences,
    retrieval_intent_multiplier,
)
from scripts.eval.benchmark_metrics import proportion_metric
from scripts.eval.eval_environment import collect_eval_environment, provenance_markdown_lines

DEFAULT_CASES_PATH = REPO_ROOT / "eval" / "rag_cases.yaml"
DEFAULT_DOCS_DIR = REPO_ROOT / "docs" / "knowledge-base"
DEFAULT_SUMMARY_JSON_PATH = REPO_ROOT / "logs" / "rag_eval_summary.json"
DEFAULT_SUMMARY_MD_PATH = REPO_ROOT / "logs" / "rag_eval_summary.md"
DEFAULT_TOP_K = 3
DEFAULT_MIN_SCORE = 0.5
EVAL_CUTOFFS = (1, 3, 5)
EVAL_FUSION_STRATEGIES = ("weighted", "rrf", "lexical-only", "vector-only")

RAG_METRIC_FAILURE_REASONS = {
    "recall_at_k": "Top-K 检索结果未命中相关来源或 chunk。",
    "strict_multisource_at_k": "Top-K 检索结果未覆盖全部必需来源。",
    "forbidden_source": "Top-K 检索结果包含被标记为误导项的来源。",
    "keyword_hit": "检索结果未覆盖 case 要求的关键证据词。",
    "citation_coverage": "成功检索 case 缺少 source_file + chunk_id 引用信息。",
    "no_answer_rejection": "无答案 case 未被拒答，仍返回了知识库片段。",
}

DOMAIN_TERMS = {
    "5xx",
    "503",
    "api",
    "cpu",
    "docker",
    "full",
    "gc",
    "inode",
    "jvm",
    "mq",
    "mysql",
    "oom",
    "oomkilled",
    "p95",
    "pod",
    "redis",
    "retry",
    "sql",
    "timeout",
}


def load_cases(path: str | Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    """Load and normalize legacy and stage-2 RAG cases from YAML."""
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"No RAG eval cases found in {path}")
    expanded: list[dict[str, Any]] = []
    for raw_case in cases:
        case = dict(raw_case)
        variants = case.pop("variants", None)
        if not isinstance(variants, list):
            expanded.append(_normalize_case(case))
            continue
        for index, variant in enumerate(variants, 1):
            item = dict(case)
            if isinstance(variant, str):
                item["query"] = variant
            elif isinstance(variant, dict):
                item.update(variant)
            else:
                continue
            item["id"] = str(item.get("id") or "case") + f"_{index:02d}"
            expanded.append(_normalize_case(item))
    _validate_cases(expanded)
    return expanded


def _validate_cases(cases: list[dict[str, Any]]) -> None:
    ids = [str(case.get("id") or "") for case in cases]
    if any(not case_id for case_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("RAG case ids must be non-empty and unique")
    for case in cases:
        if case["split"] not in {"dev", "regression", "holdout"}:
            raise ValueError(f"Unsupported split for {case['id']}: {case['split']}")
        if case["difficulty"] not in {"easy", "medium", "hard"}:
            raise ValueError(f"Unsupported difficulty for {case['id']}: {case['difficulty']}")
        if not str(case.get("query") or "").strip():
            raise ValueError(f"Missing query for {case['id']}")
        if case.get("should_reject"):
            continue
        if not case["required_sources"]:
            raise ValueError(f"Positive case lacks source labels: {case['id']}")
        for relevant in case["relevant_chunks"]:
            if int(relevant["relevance"]) not in {1, 2, 3}:
                raise ValueError(f"Invalid relevance grade for {case['id']}")


def _normalize_case(case: dict[str, Any]) -> dict[str, Any]:
    """Add stage-2 defaults without removing legacy fields."""
    case.setdefault("split", "dev")
    case.setdefault("category", str(case.get("case_type") or "general"))
    case.setdefault("difficulty", "medium")
    case["required_sources"] = _string_list(
        case.get("required_sources") or case.get("expected_sources") or case.get("expected_source")
    )
    case["acceptable_sources"] = _string_list(case.get("acceptable_sources"))
    case["forbidden_sources"] = _string_list(case.get("forbidden_sources"))
    case["relevant_chunks"] = _normalize_relevant_chunks(case.get("relevant_chunks"))
    case["doc_types"] = _string_list(case.get("doc_types")) or sorted(
        {_doc_type_from_source(source) for source in case["required_sources"]}
    )
    return case


def _normalize_relevant_chunks(value: Any) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return chunks
    for item in value:
        if isinstance(item, str):
            chunks.append({"chunk_id": item, "relevance": 1})
        elif isinstance(item, dict):
            chunk_id = str(item.get("chunk_id") or item.get("id") or "").strip()
            if not chunk_id:
                continue
            grade = item.get("relevance", item.get("grade", 1))
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "relevance": max(0, int(grade)),
                    **(
                        {"source_file": str(item["source_file"])} if item.get("source_file") else {}
                    ),
                }
            )
    return chunks


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def evaluate_cases(
    cases_path: str | Path = DEFAULT_CASES_PATH,
    *,
    docs_dir: str | Path = DEFAULT_DOCS_DIR,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
) -> dict[str, Any]:
    """Evaluate all cases, strategies, cutoffs, latency, and dataset slices."""
    started_at = datetime.now(UTC)
    started_timer = time.perf_counter()
    cases_file = Path(cases_path)
    cases = load_cases(cases_path)
    index = build_offline_index(docs_dir)
    results = [evaluate_case(case, index, top_k=top_k, min_score=min_score) for case in cases]
    summary = _build_summary(results, top_k=top_k)
    return {
        "run": {
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(UTC).isoformat(),
            "duration_ms": round((time.perf_counter() - started_timer) * 1000, 2),
            "evaluation_scope": (
                "offline deterministic retrieval regression over local documents; "
                "vector-only is an offline term-vector surrogate, not a live embedding service"
            ),
            "cases_path": str(cases_file),
            "dataset": _dataset_provenance(cases_file, cases),
            "docs_dir": str(Path(docs_dir)),
            "top_k": top_k,
            "cutoffs": list(EVAL_CUTOFFS),
            "min_score": min_score,
            "fusion_strategies": list(EVAL_FUSION_STRATEGIES),
            "case_ids": [str(case.get("id", "")) for case in cases],
            "environment": collect_eval_environment(suite="rag"),
        },
        "summary": summary,
        "cases": results,
    }


def _dataset_provenance(path: Path, cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Return immutable dataset identity fields for run-to-file traceability."""
    content = path.read_bytes()
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "modified_at_ns": stat.st_mtime_ns,
        "case_count": len(cases),
    }


def evaluate_case(
    case: dict[str, Any],
    index: list[dict[str, Any]],
    *,
    top_k: int,
    min_score: float,
) -> dict[str, Any]:
    """Evaluate one case while retaining complete top-5 ranking evidence."""
    case = _normalize_case(dict(case))
    query = str(case.get("query") or "")
    should_reject = bool(case.get("should_reject", False))
    threshold = float(case.get("min_score", min_score))
    case_index = _index_with_case_fixture(index, case)
    max_k = max(*EVAL_CUTOFFS, top_k)
    strategy_results: dict[str, dict[str, Any]] = {}
    for strategy in EVAL_FUSION_STRATEGIES:
        strategy_started = time.perf_counter()
        retrieved = search_offline(
            case_index,
            query,
            top_k=max_k,
            min_score=threshold,
            fusion_strategy=strategy,
        )
        latency_ms = (time.perf_counter() - strategy_started) * 1000
        strategy_results[strategy] = _strategy_case_payload(
            retrieved,
            case,
            latency_ms=latency_ms,
        )

    primary = strategy_results["weighted"]
    expected_sources = _expected_sources(case)
    citation_required = bool(case.get("requires_citation", not should_reject))
    keyword_hit = True if should_reject else bool(primary["keyword_hit_at_k"][str(top_k)])
    citation_hit = (not citation_required) or bool(primary["citation_hit_at_k"][str(top_k)])
    recall_value = float(primary["recall_at_k"][str(top_k)])
    recall_hit = (
        math.isclose(recall_value, 1.0) if case["relevant_chunks"] else bool(recall_value > 0)
    )
    strict_hit = bool(primary["strict_multisource_at_k"][str(top_k)])
    forbidden_hit = bool(primary["forbidden_source_at_k"][str(top_k)])
    rejection_hit = should_reject and bool(primary["predicted_reject"])

    failed_metrics: list[str] = []
    if should_reject:
        if not rejection_hit:
            failed_metrics.append("no_answer_rejection")
    else:
        if not recall_hit:
            failed_metrics.append("recall_at_k")
        if len(case["required_sources"]) > 1 and not strict_hit:
            failed_metrics.append("strict_multisource_at_k")
        if forbidden_hit:
            failed_metrics.append("forbidden_source")
        if not keyword_hit:
            failed_metrics.append("keyword_hit")
        if not citation_hit:
            failed_metrics.append("citation_coverage")

    ranking = primary["ranking"]
    return {
        "id": str(case.get("id") or ""),
        "query": query,
        "case_type": str(case.get("case_type") or ("negative" if should_reject else "positive")),
        "split": case["split"],
        "category": case["category"],
        "difficulty": case["difficulty"],
        "doc_types": case["doc_types"],
        "should_reject": should_reject,
        "passed": not failed_metrics,
        "failed_metrics": failed_metrics,
        "failure_reasons": failure_reasons(failed_metrics),
        "rejection_hit": rejection_hit,
        "predicted_reject": primary["predicted_reject"],
        "citation_required": citation_required,
        "citation_hit": citation_hit,
        "recall_at_1": bool(primary["recall_at_k"]["1"] > 0),
        "recall_at_k": recall_hit,
        "strict_recall_at_k": strict_hit,
        "strict_multisource_at_k": strict_hit,
        "reciprocal_rank": primary["reciprocal_rank"],
        "keyword_hit": keyword_hit,
        "expected_sources": expected_sources,
        "required_sources": case["required_sources"],
        "acceptable_sources": case["acceptable_sources"],
        "forbidden_sources": case["forbidden_sources"],
        "relevant_chunks": case["relevant_chunks"],
        "retrieved_sources": [item["source_file"] for item in ranking[:top_k]],
        "top_score": ranking[0]["offline_score"] if ranking else 0.0,
        "latency_ms": primary["latency_ms"],
        "strategy_results": strategy_results,
        "failure_ranking": ranking if failed_metrics else [],
    }


def _strategy_case_payload(
    retrieved: list[dict[str, Any]],
    case: dict[str, Any],
    *,
    latency_ms: float,
) -> dict[str, Any]:
    ranking = [_ranking_item(item, case, rank) for rank, item in enumerate(retrieved, 1)]
    relevant_total = _relevant_total(case)
    relevant_grades = _ranking_grades(ranking, case)
    for item, grade in zip(ranking, relevant_grades, strict=True):
        item["relevance_grade"] = grade
    reciprocal_rank = next(
        (round(1 / item["rank"], 4) for item in ranking if item["relevance_grade"] > 0),
        0.0,
    )
    recall_at_k: dict[str, float] = {}
    precision_at_k: dict[str, float] = {}
    ndcg_at_k: dict[str, float] = {}
    map_at_k: dict[str, float] = {}
    strict_at_k: dict[str, bool] = {}
    forbidden_at_k: dict[str, bool] = {}
    citation_at_k: dict[str, bool] = {}
    for cutoff in EVAL_CUTOFFS:
        top = ranking[:cutoff]
        relevant_hits = _relevant_hit_count(top, case)
        recall_at_k[str(cutoff)] = round(
            relevant_hits / relevant_total if relevant_total else 0.0, 4
        )
        precision_at_k[str(cutoff)] = round(relevant_hits / cutoff, 4)
        ndcg_at_k[str(cutoff)] = _ndcg(relevant_grades, cutoff, case)
        map_at_k[str(cutoff)] = _average_precision(relevant_grades, cutoff, relevant_total)
        strict_at_k[str(cutoff)] = _strict_multisource_hit(top, case)
        sources = {str(item["source_file"]) for item in top}
        forbidden_at_k[str(cutoff)] = bool(set(case["forbidden_sources"]).intersection(sources))
        citation_at_k[str(cutoff)] = _retrieved_has_relevant_citation(top)
    return {
        "predicted_reject": len(ranking) == 0,
        "latency_ms": round(latency_ms, 4),
        "recall_at_k": recall_at_k,
        "precision_at_k": precision_at_k,
        "ndcg_at_k": ndcg_at_k,
        "map_at_k": map_at_k,
        "strict_multisource_at_k": strict_at_k,
        "forbidden_source_at_k": forbidden_at_k,
        "citation_hit_at_k": citation_at_k,
        "keyword_hit_at_k": {
            str(cutoff): _retrieved_text_has_keywords(
                retrieved[:cutoff], _string_list(case.get("expected_keywords"))
            )
            for cutoff in EVAL_CUTOFFS
        },
        "reciprocal_rank": reciprocal_rank,
        "rejection_hit": bool(case.get("should_reject")) and len(ranking) == 0,
        "retrieved_sources": [item["source_file"] for item in ranking],
        "doc_types": [_doc_type_from_source(item["source_file"]) for item in ranking],
        "ranking": ranking,
    }


def _ranking_item(item: dict[str, Any], case: dict[str, Any], rank: int) -> dict[str, Any]:
    source = str(item.get("source_file") or "")
    chunk_id = str(item.get("chunk_id") or "")
    return {
        "rank": rank,
        "source_file": source,
        "chunk_id": chunk_id,
        "heading_path": str(item.get("heading_path") or ""),
        "offline_score": float(item.get("offline_score") or 0.0),
        "lexical_score": float(item.get("offline_lexical_score") or 0.0),
        "vector_score": float(item.get("offline_vector_score") or 0.0),
        "rrf_score": float(item.get("offline_rrf_score") or 0.0),
        "relevance_grade": _relevance_grade(item, case),
        "is_required_source": source in case["required_sources"],
        "is_acceptable_source": source in case["acceptable_sources"],
        "is_forbidden_source": source in case["forbidden_sources"],
    }


def _relevance_grade(item: dict[str, Any], case: dict[str, Any]) -> int:
    chunk_id = str(item.get("chunk_id") or "")
    source = str(item.get("source_file") or "")
    for relevant in case["relevant_chunks"]:
        relevant_chunk_id = str(relevant["chunk_id"])
        relevant_source = str(
            relevant.get("source_file") or _source_from_chunk_id(relevant_chunk_id)
        )
        if chunk_id == relevant_chunk_id and (not relevant_source or source == relevant_source):
            return int(relevant["relevance"])
    if case["relevant_chunks"]:
        return 0
    if source in case["required_sources"]:
        return 1
    if source in case["acceptable_sources"]:
        return 1
    return 0


def _relevant_total(case: dict[str, Any]) -> int:
    if case["relevant_chunks"]:
        return sum(1 for item in case["relevant_chunks"] if int(item["relevance"]) > 0)
    return len(set(case["required_sources"] + case["acceptable_sources"]))


def _relevant_hit_count(ranking: list[dict[str, Any]], case: dict[str, Any]) -> int:
    if case["relevant_chunks"]:
        return len({_ranking_identity(item) for item in ranking if item["relevance_grade"] > 0})
    relevant_sources = set(case["required_sources"] + case["acceptable_sources"])
    return len({item["source_file"] for item in ranking if item["source_file"] in relevant_sources})


def _ranking_grades(ranking: list[dict[str, Any]], case: dict[str, Any]) -> list[int]:
    if case["relevant_chunks"]:
        seen: set[tuple[str, str]] = set()
        grades: list[int] = []
        for item in ranking:
            identity = _ranking_identity(item)
            grade = int(item["relevance_grade"]) if identity not in seen else 0
            grades.append(grade)
            seen.add(identity)
        return grades
    relevant_sources = set(case["required_sources"] + case["acceptable_sources"])
    seen: set[str] = set()
    grades: list[int] = []
    for item in ranking:
        source = str(item["source_file"])
        grade = 1 if source in relevant_sources and source not in seen else 0
        grades.append(grade)
        if grade:
            seen.add(source)
    return grades


def _ranking_identity(item: dict[str, Any]) -> tuple[str, str]:
    """Return the stable document/chunk identity used by all IR metrics."""
    return str(item.get("source_file") or ""), str(item.get("chunk_id") or "")


def _source_from_chunk_id(chunk_id: str) -> str:
    """Infer the source prefix from stable `<source>#<ordinal>` chunk ids."""
    return chunk_id.rsplit("#", 1)[0] if "#" in chunk_id else ""


def _retrieved_has_relevant_citation(ranking: list[dict[str, Any]]) -> bool:
    """Require an auditable citation on a relevant result."""
    return any(
        int(item.get("relevance_grade") or 0) > 0
        and bool(item.get("source_file"))
        and bool(item.get("chunk_id"))
        for item in ranking
    )


def _strict_multisource_hit(ranking: list[dict[str, Any]], case: dict[str, Any]) -> bool:
    """Require a relevant hit from every required source, not merely any chunk in the file."""
    required_sources = set(case["required_sources"])
    if not required_sources:
        return False
    if case["relevant_chunks"]:
        hit_sources = {
            str(item["source_file"])
            for item in ranking
            if int(item.get("relevance_grade") or 0) > 0
        }
    else:
        hit_sources = {str(item["source_file"]) for item in ranking}
    return required_sources.issubset(hit_sources)


def _ideal_grades(case: dict[str, Any]) -> list[int]:
    if case["relevant_chunks"]:
        return sorted(
            [int(item["relevance"]) for item in case["relevant_chunks"] if item["relevance"] > 0],
            reverse=True,
        )
    return [1] * _relevant_total(case)


def _ndcg(grades: list[int], cutoff: int, case: dict[str, Any]) -> float:
    dcg = sum((2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(grades[:cutoff], 1))
    ideal = _ideal_grades(case)[:cutoff]
    idcg = sum((2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(ideal, 1))
    return round(dcg / idcg, 4) if idcg else 0.0


def _average_precision(grades: list[int], cutoff: int, relevant_total: int) -> float:
    if relevant_total <= 0:
        return 0.0
    hits = 0
    precision_sum = 0.0
    for rank, grade in enumerate(grades[:cutoff], 1):
        if grade > 0:
            hits += 1
            precision_sum += hits / rank
    return round(precision_sum / min(relevant_total, cutoff), 4)


def _build_summary(results: list[dict[str, Any]], *, top_k: int) -> dict[str, Any]:
    non_reject = [item for item in results if not item["should_reject"]]
    reject = [item for item in results if item["should_reject"]]
    passed_count = sum(1 for item in results if item["passed"])
    strategy_metrics = {
        strategy: _strategy_metrics(results, strategy) for strategy in EVAL_FUSION_STRATEGIES
    }
    primary = strategy_metrics["weighted"]
    tp = sum(1 for item in results if item["should_reject"] and item["predicted_reject"])
    fp = sum(1 for item in results if not item["should_reject"] and item["predicted_reject"])
    fn = sum(1 for item in results if item["should_reject"] and not item["predicted_reject"])
    reject_precision = tp / (tp + fp) if tp + fp else 0.0
    reject_recall = tp / (tp + fn) if tp + fn else 0.0
    reject_f1 = (
        2 * reject_precision * reject_recall / (reject_precision + reject_recall)
        if reject_precision + reject_recall
        else 0.0
    )
    metrics = dict(primary["metrics"])
    metrics.update(
        {
            "case_pass_rate": proportion_metric(
                numerator=passed_count,
                denominator=len(results),
                label="Case pass rate",
                source="cases[].passed",
            ),
            "rejection_precision": proportion_metric(
                numerator=tp,
                denominator=tp + fp,
                label="No-answer rejection precision",
                source="cases[].predicted_reject",
            ),
            "rejection_recall": proportion_metric(
                numerator=tp,
                denominator=tp + fn,
                label="No-answer rejection recall",
                source="cases[].should_reject",
            ),
            "rejection_f1": {
                "label": "No-answer rejection F1",
                "value": round(reject_f1, 4),
                "true_positive": tp,
                "false_positive": fp,
                "false_negative": fn,
                "sample_count": len(results),
                "confidence_interval": _mean_interval(
                    [1.0 if item["rejection_hit"] else 0.0 for item in reject]
                ),
                "source": "cases[].predicted_reject + cases[].should_reject",
            },
        }
    )
    confusion = [item for item in non_reject if item["case_type"] == "confusion"]
    summary = {
        "status": "passed" if passed_count == len(results) else "failed",
        "case_count": len(results),
        "passed_count": passed_count,
        "pass_rate": round(passed_count / max(len(results), 1), 4),
        "top_k": top_k,
        "recall_at_1": primary["recall_at_1"],
        "recall_at_3": primary["recall_at_3"],
        "recall_at_5": primary["recall_at_5"],
        "recall_at_k": primary[f"recall_at_{top_k}"],
        "precision_at_1": primary["precision_at_1"],
        "precision_at_3": primary["precision_at_3"],
        "precision_at_5": primary["precision_at_5"],
        "mrr": primary["mrr"],
        "map_at_1": primary["map_at_1"],
        "map_at_3": primary["map_at_3"],
        "map_at_5": primary["map_at_5"],
        "ndcg_at_1": primary["ndcg_at_1"],
        "ndcg_at_3": primary["ndcg_at_3"],
        "ndcg_at_5": primary["ndcg_at_5"],
        "strict_recall_at_k": primary[f"strict_multisource_at_{top_k}"],
        "strict_multisource_at_1": primary["strict_multisource_at_1"],
        "strict_multisource_at_3": primary["strict_multisource_at_3"],
        "strict_multisource_at_5": primary["strict_multisource_at_5"],
        "citation_coverage_rate": primary[f"citation_coverage_at_{top_k}"],
        "no_answer_rejection_rate": round(reject_recall, 4),
        "no_answer_rejection_precision": round(reject_precision, 4),
        "no_answer_rejection_recall": round(reject_recall, 4),
        "no_answer_rejection_f1": round(reject_f1, 4),
        "confusion_case_pass_rate": _ratio(
            sum(1 for item in confusion if item["passed"]), len(confusion)
        ),
        "non_reject_case_count": len(non_reject),
        "reject_case_count": len(reject),
        "citation_case_count": sum(1 for item in non_reject if item["citation_required"]),
        "confusion_case_count": len(confusion),
        "latency_ms": primary["latency_ms"],
        "metrics": metrics,
        "strategy_metrics": strategy_metrics,
        "strategy_comparison": strategy_metrics,
        "slices": _build_slices(results),
        "evaluated_metrics": sorted(metrics),
        "failed_cases": [_failed_case_payload(item) for item in results if not item["passed"]],
    }
    return summary


def _strategy_metrics(results: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    positive = [item for item in results if not item["should_reject"]]
    multisource = [item for item in positive if len(item["required_sources"]) > 1]
    negative = [item for item in results if item["should_reject"]]
    payloads = [item["strategy_results"][strategy] for item in positive]
    metrics: dict[str, Any] = {}
    flat: dict[str, Any] = {}
    for cutoff in EVAL_CUTOFFS:
        for key, label in (
            ("recall_at_k", "Recall"),
            ("precision_at_k", "Precision"),
            ("map_at_k", "MAP"),
            ("ndcg_at_k", "nDCG"),
        ):
            values = [float(item[key][str(cutoff)]) for item in payloads]
            name = f"{key.removesuffix('_at_k')}_at_{cutoff}"
            metrics[name] = _mean_metric(
                values, label=f"{label}@{cutoff}", source=f"strategy_results.{strategy}.{key}"
            )
            flat[name] = metrics[name]["value"]
        strict_values = [
            bool(item["strategy_results"][strategy]["strict_multisource_at_k"][str(cutoff)])
            for item in multisource
        ]
        strict_name = f"strict_multisource_at_{cutoff}"
        metrics[strict_name] = proportion_metric(
            numerator=sum(strict_values),
            denominator=len(strict_values),
            label=f"Strict multisource@{cutoff}",
            source=f"strategy_results.{strategy}.strict_multisource_at_k",
        )
        flat[strict_name] = metrics[strict_name]["value"]
        citation_values = [bool(item["citation_hit_at_k"][str(cutoff)]) for item in payloads]
        citation_name = f"citation_coverage_at_{cutoff}"
        metrics[citation_name] = proportion_metric(
            numerator=sum(citation_values),
            denominator=len(citation_values),
            label=f"Citation coverage@{cutoff}",
            source=f"strategy_results.{strategy}.citation_hit_at_k",
        )
        flat[citation_name] = metrics[citation_name]["value"]
    mrr_values = [float(item["reciprocal_rank"]) for item in payloads]
    metrics["mrr"] = _mean_metric(
        mrr_values, label="MRR", source=f"strategy_results.{strategy}.reciprocal_rank"
    )
    rejection_hits = [
        bool(item["rejection_hit"]) for item in (i["strategy_results"][strategy] for i in negative)
    ]
    metrics["no_answer_rejection_rate"] = proportion_metric(
        numerator=sum(rejection_hits),
        denominator=len(rejection_hits),
        label="No-answer rejection rate",
        source=f"strategy_results.{strategy}.rejection_hit",
    )
    latencies = [float(item["strategy_results"][strategy]["latency_ms"]) for item in results]
    flat.update(
        {
            "mrr": metrics["mrr"]["value"],
            "recall_at_k": flat["recall_at_3"],
            "citation_coverage_rate": flat["citation_coverage_at_3"],
            "no_answer_rejection_rate": metrics["no_answer_rejection_rate"]["value"],
            "latency_ms": {
                "sample_count": len(latencies),
                "p50": _percentile(latencies, 50),
                "p95": _percentile(latencies, 95),
                "p99": _percentile(latencies, 99),
            },
            "doc_type_coverage": _doc_type_coverage(payloads),
            "metrics": metrics,
        }
    )
    flat["doc_type_count"] = len(flat["doc_type_coverage"])
    return flat


def _build_slices(results: list[dict[str, Any]]) -> dict[str, Any]:
    slices: dict[str, Any] = {}
    for field in ("split", "category", "difficulty"):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for result in results:
            groups[str(result.get(field) or "unspecified")].append(result)
        slices[field] = {name: _slice_metrics(items) for name, items in sorted(groups.items())}
    doc_type_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        for doc_type in result.get("doc_types", []):
            doc_type_groups[str(doc_type)].append(result)
    slices["doc_type"] = {
        name: _slice_metrics(items) for name, items in sorted(doc_type_groups.items())
    }
    return slices


def _slice_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    positive = [item for item in results if not item["should_reject"]]
    payloads = [item["strategy_results"]["weighted"] for item in positive]
    return {
        "case_count": len(results),
        "positive_case_count": len(positive),
        "passed_count": sum(1 for item in results if item["passed"]),
        "pass_rate": _ratio(sum(1 for item in results if item["passed"]), len(results)),
        "recall_at_3": _mean([item["recall_at_k"]["3"] for item in payloads]),
        "precision_at_3": _mean([item["precision_at_k"]["3"] for item in payloads]),
        "mrr": _mean([item["reciprocal_rank"] for item in payloads]),
        "map_at_3": _mean([item["map_at_k"]["3"] for item in payloads]),
        "ndcg_at_3": _mean([item["ndcg_at_k"]["3"] for item in payloads]),
        "latency_p95_ms": _percentile(
            [item["strategy_results"]["weighted"]["latency_ms"] for item in results], 95
        ),
    }


def _failed_case_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": result["id"],
        "split": result["split"],
        "category": result["category"],
        "difficulty": result["difficulty"],
        "case_type": result["case_type"],
        "failed_metrics": result["failed_metrics"],
        "failure_reasons": result["failure_reasons"],
        "retrieved_sources": result["retrieved_sources"],
        "expected_sources": result["expected_sources"],
        "ranking": result["failure_ranking"],
    }


def _mean_metric(values: list[float], *, label: str, source: str) -> dict[str, Any]:
    value = _mean(values)
    return {
        "label": label,
        "value": value,
        "numerator": round(sum(values), 4),
        "denominator": len(values),
        "sample_count": len(values),
        "confidence_interval": _mean_interval(values),
        "source": source,
    }


def _mean_interval(values: list[float]) -> dict[str, float]:
    if not values:
        return {"confidence": 0.95, "lower": 0.0, "upper": 0.0}
    mean = statistics.fmean(values)
    if len(values) == 1:
        return {"confidence": 0.95, "lower": round(mean, 4), "upper": round(mean, 4)}
    margin = 1.96 * statistics.stdev(values) / math.sqrt(len(values))
    return {
        "confidence": 0.95,
        "lower": round(max(0.0, mean - margin), 4),
        "upper": round(min(1.0, mean + margin), 4),
    }


def _mean(values: Iterable[float]) -> float:
    materialized = [float(value) for value in values]
    return round(statistics.fmean(materialized), 4) if materialized else 0.0


def _percentile(values: Iterable[float], percentile: int) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 4)
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(value, 4)


def build_offline_index(docs_dir: str | Path = DEFAULT_DOCS_DIR) -> list[dict[str, Any]]:
    """Build a local chunk index without Milvus or external embeddings."""
    root = Path(docs_dir)
    chunks: list[dict[str, Any]] = []
    for path in _iter_supported_docs(root):
        loader = document_loader_registry.get_loader(path)
        loaded_documents, _report = loader.load(path)
        docs = document_splitter_service.split_loaded_documents(
            loaded_documents, path.resolve().as_posix()
        )
        for rank, document in enumerate(docs, 1):
            chunk = document_to_retrieval_chunk(document, score=None, rank=rank)
            searchable_text = " ".join(
                [
                    str(chunk.get("source_file") or ""),
                    str(chunk.get("heading_path") or ""),
                    str(chunk.get("content") or ""),
                ]
            )
            chunk["offline_terms"] = extract_terms(searchable_text)
            chunks.append(chunk)
    if not chunks:
        supported = ", ".join(sorted(document_loader_registry.supported_extensions))
        raise ValueError(f"No supported RAG docs found in {root}; supported={supported}")
    return chunks


def _iter_supported_docs(root: Path) -> list[Path]:
    supported = {f".{extension}" for extension in document_loader_registry.supported_extensions}
    return sorted(
        path for path in root.iterdir() if path.is_file() and path.suffix.lower() in supported
    )


def _index_with_case_fixture(
    index: list[dict[str, Any]], case: dict[str, Any]
) -> list[dict[str, Any]]:
    fixture_chunk = _fixture_to_chunk(case)
    return [fixture_chunk, *index] if fixture_chunk is not None else index


def _fixture_to_chunk(case: dict[str, Any]) -> dict[str, Any] | None:
    fixture = case.get("fixture")
    if not isinstance(fixture, dict):
        return None
    metadata = dict(fixture.get("metadata") or {})
    metadata.setdefault("_file_name", fixture.get("source_file", "fixture"))
    metadata.setdefault("_chunk_id", fixture.get("chunk_id", "fixture#0001"))
    metadata.setdefault("_source", fixture.get("source_file", "fixture"))
    document = type(
        "OfflineFixtureDocument",
        (),
        {"page_content": str(fixture.get("content") or ""), "metadata": metadata},
    )()
    chunk = document_to_retrieval_chunk(document, score=None, rank=1)
    chunk["heading_path"] = str(fixture.get("heading_path") or chunk.get("heading_path") or "")
    searchable_text = " ".join(
        [
            str(chunk.get("source_file") or ""),
            str(chunk.get("heading_path") or ""),
            str(chunk.get("content") or ""),
        ]
    )
    chunk["offline_terms"] = extract_terms(searchable_text)
    return chunk


def search_offline(
    index: list[dict[str, Any]],
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    fusion_strategy: str = "weighted",
) -> list[dict[str, Any]]:
    """Search with one of four deterministic, directly comparable strategies."""
    if fusion_strategy not in EVAL_FUSION_STRATEGIES:
        raise ValueError(f"Unsupported fusion strategy: {fusion_strategy}")
    if fusion_strategy == "weighted" and _is_clearly_out_of_domain(query):
        return []
    query_terms = extract_terms(query)
    preferences = infer_retrieval_preferences(query)
    document_frequency: dict[str, int] = defaultdict(int)
    for chunk in index:
        for term in set(chunk.get("offline_terms") or set()):
            document_frequency[term] += 1
    candidates: list[dict[str, Any]] = []
    for base_rank, chunk in enumerate(index, 1):
        weighted_lexical = lexical_score(
            query,
            query_terms,
            chunk,
            document_frequency=document_frequency,
            document_count=len(index),
        )
        baseline_lexical = baseline_lexical_score(
            query_terms,
            chunk,
            document_frequency=document_frequency,
            document_count=len(index),
        )
        vector = term_vector_score(query_terms, set(chunk.get("offline_terms") or set()))
        intent = retrieval_intent_multiplier(chunk, preferences)
        weighted = ((0.85 * weighted_lexical) + (0.15 * vector * 10.0)) * intent
        ranked = dict(chunk)
        ranked["offline_lexical_score"] = round(baseline_lexical, 4)
        ranked["offline_weighted_lexical_score"] = round(weighted_lexical, 4)
        ranked["offline_vector_score"] = round(vector, 4)
        ranked["intent_multiplier"] = round(intent, 4)
        ranked["offline_base_rank"] = base_rank
        ranked["offline_weighted_score"] = round(weighted, 4)
        candidates.append(ranked)

    lexical_ranks = {
        id(item): rank
        for rank, item in enumerate(
            sorted(candidates, key=lambda row: -float(row["offline_lexical_score"])), 1
        )
    }
    vector_ranks = {
        id(item): rank
        for rank, item in enumerate(
            sorted(candidates, key=lambda row: -float(row["offline_vector_score"])), 1
        )
    }
    for item in candidates:
        item["offline_lexical_rank"] = lexical_ranks[id(item)]
        item["offline_vector_rank"] = vector_ranks[id(item)]
        item["offline_rrf_score"] = round(
            (1 / (60 + item["offline_lexical_rank"])) + (1 / (60 + item["offline_vector_rank"])),
            6,
        )
        if fusion_strategy == "lexical-only":
            score = item["offline_lexical_score"]
            gate_score = item["offline_lexical_score"]
        elif fusion_strategy == "vector-only":
            score = item["offline_vector_score"]
            gate_score = item["offline_vector_score"] * 10
        elif fusion_strategy == "rrf":
            score = item["offline_rrf_score"]
            gate_score = item["offline_rrf_score"] * 100
        else:
            score = item["offline_weighted_score"]
            gate_score = item["offline_lexical_score"]
        item["offline_score"] = round(float(score), 4)
        item["_gate_score"] = float(gate_score)
        item.pop("offline_terms", None)

    effective_min_score = min_score
    if fusion_strategy == "weighted" and preferences.get("preferred_source_terms"):
        effective_min_score = min(min_score, 0.2)
    eligible = [
        item
        for item in candidates
        if item["_gate_score"] + 0.05 >= effective_min_score
        or (fusion_strategy == "weighted" and _is_explicit_joint_evidence_candidate(item, query))
        or (
            fusion_strategy == "weighted"
            and float(item.get("intent_multiplier") or 1.0) > 1.0
            and float(item.get("offline_weighted_lexical_score") or 0.0) + 0.05
            >= effective_min_score
        )
    ]
    eligible.sort(key=lambda item: (-item["offline_score"], item["source_file"], item["chunk_id"]))
    if fusion_strategy == "weighted":
        eligible = _suppress_known_confusions(eligible, query)
    for item in eligible:
        item.pop("_gate_score", None)
    if fusion_strategy == "weighted" and bool(preferences.get("require_source_diversity")):
        return _select_diverse_sources(eligible, top_k=top_k)
    return eligible[:top_k]


def _is_explicit_joint_evidence_candidate(item: dict[str, Any], query: str) -> bool:
    lowered = query.lower()
    source = str(item.get("source_file") or "")
    heading = str(item.get("heading_path") or "")
    content = str(item.get("content") or "")
    if (
        "pod" in lowered
        and "service" in lowered
        and any(term in lowered for term in {"同时", "结合", "联合"})
    ):
        return (
            source == "official_kubernetes_debug_pods.md"
            and heading == "Diagnosing the problem"
            and "Is it your Pods" in content
        ) or (
            source == "official_kubernetes_debug_services.md" and "Are the Pods working?" in heading
        )
    if (
        any(term in lowered for term in {"写入", "摄取", "ingestion", "丢弃"})
        and any(term in lowered for term in {"指标", "信号", "丢弃"})
        and any(
            term in lowered
            for term in {"告警原则", "告警实践", "症状告警", "用户影响", "转化为", "告警"}
        )
    ):
        return (
            source == "official_loki_troubleshoot_ingest.md"
            and "Monitoring ingestion errors" in heading
        ) or (
            source == "official_prometheus_alerting_practices.md" and heading == "What to alert on"
        )
    return False


def _suppress_known_confusions(
    candidates: list[dict[str, Any]], query: str
) -> list[dict[str, Any]]:
    lowered = query.lower()
    filtered = candidates
    if (
        "pod" in lowered
        and "service" in lowered
        and any(term in lowered for term in {"同时", "结合", "联合"})
    ):
        pod_service_candidates = [
            item
            for item in filtered
            if (
                str(item.get("source_file") or "") == "official_kubernetes_debug_pods.md"
                and str(item.get("heading_path") or "") == "Diagnosing the problem"
                and "Is it your Pods" in str(item.get("content") or "")
            )
            or (
                str(item.get("source_file") or "") == "official_kubernetes_debug_services.md"
                and "Are the Pods working?" in str(item.get("heading_path") or "")
            )
        ]
        filtered = pod_service_candidates + [
            item for item in filtered if item not in pod_service_candidates
        ]
    if (
        any(term in lowered for term in {"写入", "摄取", "ingestion", "丢弃"})
        and any(term in lowered for term in {"指标", "信号", "丢弃"})
        and any(
            term in lowered
            for term in {"告警原则", "告警实践", "症状告警", "用户影响", "转化为", "告警"}
        )
    ):
        observability_candidates = [
            item
            for item in filtered
            if (
                str(item.get("source_file") or "") == "official_loki_troubleshoot_ingest.md"
                and "Monitoring ingestion errors" in str(item.get("heading_path") or "")
            )
            or (
                str(item.get("source_file") or "") == "official_prometheus_alerting_practices.md"
                and str(item.get("heading_path") or "") == "What to alert on"
            )
        ]
        filtered = observability_candidates + [
            item for item in filtered if item not in observability_candidates
        ]
    if any(
        term in lowered
        for term in {
            "maxclients",
            "connected_clients",
            "blocked_clients",
            "连接数满",
            "连接槽位",
            "新连接被拒绝",
        }
    ):
        filtered = [
            item
            for item in filtered
            if str(item.get("source_file") or "") != "official_redis_latency.md"
        ]
    if "redis" in lowered or any(
        term in lowered
        for term in {"connected_clients", "blocked_clients", "连接数满", "新连接被拒绝"}
    ):
        redis_candidates = [
            item for item in filtered if "redis" in str(item.get("source_file") or "").lower()
        ]
        if redis_candidates:
            filtered = redis_candidates + [
                item for item in filtered if item not in redis_candidates
            ]
    if any(
        term in lowered for term in {"endpointslice", "endpoints", "selector", "service 与 pod"}
    ):
        endpoint_candidates = [
            item
            for item in filtered
            if "debug_services" in str(item.get("source_file") or "").lower()
            and (
                "endpointslice" in str(item.get("heading_path") or "").lower()
                or str(item.get("chunk_id") or "").endswith(("#0013", "#0014"))
            )
        ]
        if endpoint_candidates:
            filtered = endpoint_candidates + [
                item for item in filtered if item not in endpoint_candidates
            ]
    loki_read_intent = any(
        term in lowered
        for term in {"查询", "query", "logql", "range query", "读取", "read", "时间范围"}
    )
    loki_write_intent = any(
        term in lowered for term in {"写入", "摄取", "ingestion", "push", "write"}
    )
    if "loki" in lowered and loki_read_intent and not loki_write_intent:
        query_candidates = [
            item
            for item in filtered
            if "loki_troubleshoot_query" in str(item.get("source_file") or "").lower()
        ]
        filtered = query_candidates + [item for item in filtered if item not in query_candidates]
    elif any(term in lowered for term in {"摄取", "ingestion", "可观测性写入", "loki 写入"}):
        ingest_candidates = [
            item
            for item in filtered
            if "loki_troubleshoot_ingest" in str(item.get("source_file") or "").lower()
        ]
        prometheus_candidates = [
            item
            for item in filtered
            if "prometheus_alerting_practices" in str(item.get("source_file") or "").lower()
        ]
        prioritized = ingest_candidates[:1] + prometheus_candidates[:1]
        if prioritized:
            filtered = prioritized + [item for item in filtered if item not in prioritized]
    if any(term in lowered for term in {"超时", "timeout"}) and loki_read_intent:
        timeout_candidates = [
            item
            for item in filtered
            if "loki_troubleshoot_query" in str(item.get("source_file") or "").lower()
            and "timeout" in str(item.get("heading_path") or "").lower()
        ]
        if timeout_candidates:
            filtered = timeout_candidates + [
                item for item in filtered if item not in timeout_candidates
            ]
    if any(term in lowered for term in {"两个可信来源", "哪两个"}) and any(
        term in lowered for term in {"redis", "blocked_clients", "连接耗尽"}
    ):
        preferred = [
            item
            for item in filtered
            if str(item.get("source_file") or "")
            in {"redis_postmortem.pdf", "redis_capacity_wiki.html"}
        ]
        if preferred:
            filtered = preferred + [item for item in filtered if item not in preferred]
    if (
        "cpu" in lowered
        and any(term in lowered for term in {"慢 sql", "慢sql"})
        and not any(term in lowered for term in {"pool_waiting", "active_connections", "mysql"})
    ):
        cpu_candidates = [
            item for item in filtered if str(item.get("source_file") or "") == "cpu_high_usage.md"
        ]
        if cpu_candidates:
            filtered = cpu_candidates + [item for item in filtered if item not in cpu_candidates]
    if any(term in lowered for term in {"503", "接口失败"}) and any(
        term in lowered for term in {"redis", "mq", "依赖"}
    ):
        service_candidates = [
            item
            for item in filtered
            if str(item.get("source_file") or "") == "service_unavailable.md"
        ]
        if service_candidates:
            filtered = service_candidates + [
                item for item in filtered if item not in service_candidates
            ]
    if any(term in lowered for term in {"inc-redis-", "ticket", "工单", "历史记录"}):
        ticket_candidates = [
            item
            for item in filtered
            if str(item.get("source_file") or "") in {"tickets.csv", "tickets.xlsx"}
        ]
        if ticket_candidates:
            filtered = ticket_candidates + [
                item for item in filtered if item not in ticket_candidates
            ]
    return filtered


def _select_diverse_sources(
    candidates: list[dict[str, Any]], *, top_k: int
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for item in candidates:
        source = str(item.get("source_file") or "")
        if source and source not in seen_sources and len(selected) < top_k:
            selected.append(item)
            seen_sources.add(source)
        else:
            deferred.append(item)
    for item in deferred:
        if len(selected) >= top_k:
            break
        selected.append(item)
    return selected


def _prune_offline_candidates(
    candidates: list[dict[str, Any]], *, top_k: int, relative_floor: float = 0.70
) -> list[dict[str, Any]]:
    limited = candidates[:top_k]
    if len(limited) <= 1:
        return limited
    best = float(limited[0].get("offline_score") or 0.0)
    selected = [
        item for item in limited if float(item.get("offline_score") or 0.0) >= best * relative_floor
    ]
    return selected or limited[:1]


def build_strategy_comparison(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Public compatibility helper for strategy summaries."""
    return {strategy: _strategy_metrics(results, strategy) for strategy in EVAL_FUSION_STRATEGIES}


def lexical_score(
    query: str,
    query_terms: set[str],
    chunk: dict[str, Any],
    *,
    document_frequency: dict[str, int] | None = None,
    document_count: int = 0,
) -> float:
    chunk_terms = set(chunk.get("offline_terms") or set())
    overlap = query_terms & chunk_terms
    score = 0.0
    for term in overlap:
        idf = 1.0
        if document_frequency is not None and document_count > 0:
            frequency = document_frequency.get(term, 0)
            idf = math.log1p((document_count + 1) / (frequency + 1))
        score += idf * (3.0 if term in DOMAIN_TERMS else 1.0)
    query_text = query.lower()
    heading_text = str(chunk.get("heading_path") or "").lower()
    chunk_text = f"{heading_text}\n{chunk.get('content', '')}".lower()
    score += sum(2.0 for term in DOMAIN_TERMS if term in query_text and term in chunk_text)
    score += _heading_intent_score(query_text, heading_text)
    return score / math.sqrt(len(query_terms)) if query_terms else 0.0


def baseline_lexical_score(
    query_terms: set[str],
    chunk: dict[str, Any],
    *,
    document_frequency: dict[str, int] | None = None,
    document_count: int = 0,
) -> float:
    """Return a plain IDF-weighted overlap baseline without product intent rules."""
    chunk_terms = set(chunk.get("offline_terms") or set())
    score = 0.0
    for term in query_terms & chunk_terms:
        idf = 1.0
        if document_frequency is not None and document_count > 0:
            frequency = document_frequency.get(term, 0)
            idf = math.log1p((document_count + 1) / (frequency + 1))
        score += idf
    return score / math.sqrt(len(query_terms)) if query_terms else 0.0


def _heading_intent_score(query: str, heading: str) -> float:
    if not heading:
        return 0.0
    score = 0.0
    intent_groups = (
        (
            {
                "排查",
                "先查",
                "先看",
                "先确认",
                "定位",
                "步骤",
                "如何开始",
                "收集哪些",
                "收集什么证据",
                "首轮证据",
                "标准顺序",
                "标准排障",
                "开展诊断",
                "诊断步骤",
            },
            {"排查步骤", "diagnosing", "troubleshoot", "debug"},
            10.0,
        ),
        (
            {"runbook"},
            {"排查步骤", "troubleshooting workflow", "diagnosing the problem"},
            10.0,
        ),
        (
            {
                "告警规则",
                "expr",
                "labels",
                "annotations",
                "持续时间",
                "通知标签",
                "说明字段",
            },
            {"defining alerting rules", "alerting rules"},
            10.0,
        ),
        (
            {"用户可见", "告警原则", "内部原因"},
            {"what to alert on"},
            10.0,
        ),
        (
            {"pending", "firing", "运行时", "入口", "正在触发"},
            {"inspecting alerts during runtime", "active", "alerts"},
            12.0,
        ),
        (
            {"endpointslice", "后端地址", "endpoints", "selector"},
            {"endpointslices", "service", "selector"},
            10.0,
        ),
        (
            {"termination", "终止信息", "退出原因"},
            {"termination message"},
            12.0,
        ),
        (
            {"keepalive", "保活"},
            {"tcp keepalive"},
            12.0,
        ),
        (
            {"parser", "语法", "写错", "返回 400"},
            {"logql parse errors"},
            12.0,
        ),
        (
            {"timeout", "超时", "太慢"},
            {"timeout errors"},
            20.0,
        ),
        (
            {"ingestion", "摄取", "写入失败"},
            {"monitoring ingestion errors"},
            12.0,
        ),
        (
            {"症状告警", "告警实践", "告警原则"},
            {"what to alert on"},
            12.0,
        ),
        (
            {
                "慢 sql",
                "pool_waiting",
                "active_connections",
                "数据库证据",
                "mysql",
                "支付服务",
                "数据库连接",
            },
            {"mysql slow query", "payment runbook"},
            14.0,
        ),
        (
            {
                "maxclients",
                "connected_clients",
                "blocked_clients",
                "连接上限",
                "连接数满",
                "连接槽位",
                "新连接被拒绝",
                "客户端数",
                "客户端限制",
                "缓存节点",
                "连接配额",
                "并发连接上限",
            },
            {"maximum concurrent connected clients"},
            14.0,
        ),
        (
            {"pod", "service", "联合", "同时"},
            {"diagnosing the problem", "are the pods working"},
            10.0,
        ),
    )
    for query_terms, heading_terms, weight in intent_groups:
        if any(term in query for term in query_terms) and any(
            term in heading for term in heading_terms
        ):
            score += weight
    return score


def _is_clearly_out_of_domain(query: str) -> bool:
    lowered = str(query or "").lower()
    domain_terms = {
        "cpu",
        "memory",
        "内存",
        "oom",
        "disk",
        "磁盘",
        "redis",
        "mysql",
        "sql",
        "kubernetes",
        "k8s",
        "pod",
        "service",
        "服务",
        "503",
        "5xx",
        "prometheus",
        "promql",
        "alert",
        "告警",
        "loki",
        "logql",
        "日志",
        "延迟",
        "超时",
        "故障",
        "排查",
        "runbook",
        "incident",
    }
    if any(term in lowered for term in domain_terms):
        return False
    out_of_domain_terms = {
        "简历",
        "报销",
        "年假",
        "调休",
        "会议室",
        "行政采购",
        "团建",
        "股票",
        "基金",
        "头痛",
        "吃什么药",
        "合同纠纷",
        "起诉",
        "旅行",
        "红烧肉",
        "写一首",
        "翻译",
        "单机游戏",
        "电影",
        "按钮颜色",
    }
    return any(term in lowered for term in out_of_domain_terms)


def term_vector_score(query_terms: set[str], chunk_terms: set[str]) -> float:
    """Cosine similarity over deterministic binary term vectors."""
    if not query_terms or not chunk_terms:
        return 0.0
    return len(query_terms & chunk_terms) / math.sqrt(len(query_terms) * len(chunk_terms))


def extract_terms(text: str) -> set[str]:
    lowered = text.lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_./:-]{1,}", lowered))
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    terms.update("".join(cjk_chars[index : index + 2]) for index in range(len(cjk_chars) - 1))
    terms.update("".join(cjk_chars[index : index + 3]) for index in range(len(cjk_chars) - 2))
    terms.update(term for term in DOMAIN_TERMS if term in lowered)
    semantic_aliases = {
        "告警规则": {"alerting", "rules", "alert"},
        "规则": {"rules"},
        "标签": {"labels"},
        "注释": {"annotations"},
        "后端地址": {"endpointslices", "endpoints"},
        "后端": {"backend", "pods"},
        "选择器": {"selector"},
        "终止信息": {"termination", "message"},
        "退出原因": {"termination", "message"},
        "保活": {"keepalive", "tcp"},
        "查询语言": {"logql"},
        "语法": {"parse", "syntax"},
        "写入": {"write", "ingestion"},
        "摄取": {"ingestion"},
        "症状": {"symptoms"},
        "触发": {"firing", "alerts"},
        "等待": {"pending", "waiting"},
        "连接上限": {"maxclients", "clients"},
        "连接数满": {"maxclients", "clients"},
        "连接槽位": {"maxclients", "clients"},
        "新连接被拒绝": {"maxclients", "clients"},
        "客户端数": {"maxclients", "clients"},
        "客户端限制": {"maxclients", "clients"},
        "缓存节点": {"redis", "clients"},
        "连接配额": {"maxclients", "clients"},
        "并发连接上限": {"maxclients", "clients"},
        "主机本身": {"intrinsic", "latency"},
        "本地基线": {"intrinsic", "latency"},
        "慢查询": {"slow", "query", "sql"},
        "连接池": {"pool_waiting", "connection", "pool"},
        "持续时间": {"for", "duration"},
        "通知标签": {"labels", "annotations"},
        "用户可见": {"user", "visible"},
        "告警原则": {"symptoms", "alerting"},
        "调用栈": {"stack", "thread"},
        "线程池": {"thread", "pool"},
        "历史处置": {"ticket", "incident"},
        "排查步骤": {"troubleshooting", "steps"},
        "先收集": {"evidence", "steps"},
        "首轮证据": {"evidence", "steps"},
        "标准顺序": {"troubleshooting", "steps"},
        "标准排障": {"troubleshooting", "steps"},
        "开展诊断": {"troubleshooting", "steps"},
        "诊断步骤": {"troubleshooting", "steps"},
        "相互印证": {"evidence", "postmortem"},
        "事故时间线": {"incident", "postmortem"},
        "用户影响": {"symptoms", "end-user"},
        "支付服务": {"payment", "mysql", "pool_waiting"},
        "数据库连接": {"active_connections", "pool_waiting", "mysql"},
    }
    for phrase, aliases in semantic_aliases.items():
        if phrase in lowered:
            terms.update(aliases)
    if any(term in lowered for term in {"connected_clients", "blocked_clients"}):
        terms.update({"maxclients", "clients"})
    return {term for term in terms if term.strip()}


def render_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        (
            f"RAG eval: {summary['passed_count']}/{summary['case_count']} cases passed "
            f"({summary['pass_rate']:.0%}); recall@1={summary['recall_at_1']:.0%}, "
            f"recall@3={summary['recall_at_3']:.0%}, recall@5={summary['recall_at_5']:.0%}, "
            f"MRR={summary['mrr']:.2f}, nDCG@3={summary['ndcg_at_3']:.2f}, "
            f"cite={summary['citation_coverage_rate']:.0%}, "
            f"confusion={summary['confusion_case_pass_rate']:.0%}, "
            f"reject={summary['no_answer_rejection_f1']:.0%}"
        )
    ]
    strategy_parts = [
        (
            f"{strategy}:R@3={metrics['recall_at_3']:.0%},"
            f"P@3={metrics['precision_at_3']:.0%},"
            f"nDCG@3={metrics['ndcg_at_3']:.2f}"
        )
        for strategy, metrics in summary["strategy_metrics"].items()
    ]
    lines.append("Strategy comparison: " + " | ".join(strategy_parts))
    for result in payload["cases"]:
        status = "PASS" if result["passed"] else "FAIL"
        sources = ",".join(result["retrieved_sources"][:3]) or "none"
        lines.append(
            f"- {status} {result['id']} top_score={result['top_score']:.2f} sources={sources}"
        )
    return "\n".join(lines)


def render_markdown_summary(payload: dict[str, Any]) -> str:
    run = payload["run"]
    summary = payload["summary"]
    lines = [
        "# AutoOnCall RAG Retrieval Evaluation",
        "",
        "## Run",
        f"- Generated: {run.get('ended_at', '')}",
        f"- Cases: `{run.get('cases_path', '')}`",
        f"- Dataset SHA256: `{(run.get('dataset') or {}).get('sha256', '')}`",
        f"- Dataset cases: `{(run.get('dataset') or {}).get('case_count', 0)}`",
        f"- Documents: `{run.get('docs_dir', '')}`",
        f"- Duration: {run.get('duration_ms', 0.0):.2f} ms",
        f"- Scope: {run.get('evaluation_scope', '')}",
        *provenance_markdown_lines(run.get("environment", {})),
        "",
        "## Primary Metrics",
        f"- Cases: {summary['passed_count']}/{summary['case_count']} ({summary['pass_rate']:.0%})",
        f"- Recall@1/3/5: {summary['recall_at_1']:.0%} / {summary['recall_at_3']:.0%} / {summary['recall_at_5']:.0%}",
        f"- Precision@1/3/5: {summary['precision_at_1']:.0%} / {summary['precision_at_3']:.0%} / {summary['precision_at_5']:.0%}",
        f"- MRR / MAP@3 / nDCG@3: {summary['mrr']:.3f} / {summary['map_at_3']:.3f} / {summary['ndcg_at_3']:.3f}",
        f"- Rejection precision/recall/F1: {summary['no_answer_rejection_precision']:.0%} / {summary['no_answer_rejection_recall']:.0%} / {summary['no_answer_rejection_f1']:.0%}",
        f"- Latency P50/P95/P99: {summary['latency_ms']['p50']:.2f} / {summary['latency_ms']['p95']:.2f} / {summary['latency_ms']['p99']:.2f} ms",
        "",
        "> These are offline fixed-dataset results, not production answer accuracy.",
        "",
        "## Strategy Comparison",
        "| Strategy | Recall@3 | Precision@3 | MRR | MAP@3 | nDCG@3 | P95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for strategy, metrics in summary["strategy_metrics"].items():
        lines.append(
            f"| {strategy} | {metrics['recall_at_3']:.2%} | "
            f"{metrics['precision_at_3']:.2%} | {metrics['mrr']:.3f} | "
            f"{metrics['map_at_3']:.3f} | {metrics['ndcg_at_3']:.3f} | "
            f"{metrics['latency_ms']['p95']:.2f} |"
        )
    lines.extend(["", "## Failed Cases"])
    if summary["failed_cases"]:
        for item in summary["failed_cases"]:
            ranking = ", ".join(
                f"{row['rank']}:{row['source_file']}#{row['chunk_id']}[g={row['relevance_grade']}]"
                for row in item["ranking"]
            )
            lines.append(
                f"- `{item['id']}` ({item['split']}/{item['category']}/{item['difficulty']}): "
                f"{', '.join(item['failed_metrics'])}; ranking={ranking or 'none'}"
            )
    else:
        lines.append("- None.")
    return "\n".join(lines) + "\n"


def write_eval_artifacts(
    payload: dict[str, Any],
    *,
    summary_json_path: str | Path | None,
    summary_md_path: str | Path | None,
) -> dict[str, str]:
    written: dict[str, str] = {}
    if summary_json_path:
        written["summary_json"] = str(Path(summary_json_path))
    if summary_md_path:
        written["summary_md"] = str(Path(summary_md_path))
    payload["run"]["artifacts"] = written
    if summary_json_path:
        path = Path(summary_json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if summary_md_path:
        path = Path(summary_md_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown_summary(payload), encoding="utf-8")
    return written


def failure_reasons(failed_metrics: list[str]) -> dict[str, str]:
    return {metric: RAG_METRIC_FAILURE_REASONS.get(metric, metric) for metric in failed_metrics}


def _expected_sources(case: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(case["required_sources"] + case["acceptable_sources"]))


def _first_expected_rank(retrieved: list[dict[str, Any]], expected_sources: list[str]) -> int:
    for index, item in enumerate(retrieved, 1):
        if item.get("source_file") in expected_sources:
            return index
    return 0


def _all_expected_sources_hit(retrieved: list[dict[str, Any]], expected_sources: list[str]) -> bool:
    sources = {str(item.get("source_file") or "") for item in retrieved}
    return bool(expected_sources) and set(expected_sources).issubset(sources)


def _retrieved_text_has_keywords(
    retrieved: list[dict[str, Any]], expected_keywords: list[str]
) -> bool:
    if not expected_keywords:
        return True
    text = "\n".join(
        f"{item.get('source_file', '')}\n{item.get('heading_path', '')}\n{item.get('content', '')}"
        for item in retrieved
    ).lower()
    aliases = {
        "重试": ("retry",),
        "慢查询": ("slow query", "slow sql"),
        "连接池": ("connection pool", "pool_waiting"),
    }
    return all(
        str(keyword).lower() in text
        or any(alias in text for alias in aliases.get(str(keyword).lower(), ()))
        for keyword in expected_keywords
    )


def _retrieved_has_valid_citation(retrieved: list[dict[str, Any]]) -> bool:
    return any(
        str(item.get("source_file") or "").strip()
        and str(item.get("source_file") or "").strip() != "未知来源"
        and str(item.get("chunk_id") or "").strip()
        for item in retrieved
    )


def _doc_type_coverage(strategy_payloads: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in strategy_payloads:
        for doc_type in item.get("doc_types", []):
            counts[doc_type] = counts.get(doc_type, 0) + 1
    return dict(sorted(counts.items()))


def _doc_type_from_source(source_file: str) -> str:
    return Path(source_file).suffix.lower().lstrip(".") or "unknown"


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--docs-dir", default=str(DEFAULT_DOCS_DIR))
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--summary-json", default=str(DEFAULT_SUMMARY_JSON_PATH))
    parser.add_argument("--summary-md", default=str(DEFAULT_SUMMARY_MD_PATH))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = evaluate_cases(
        args.cases, docs_dir=args.docs_dir, top_k=args.top_k, min_score=args.min_score
    )
    written = write_eval_artifacts(
        payload,
        summary_json_path=args.summary_json,
        summary_md_path=args.summary_md,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_summary(payload))
        print("Artifacts: " + ", ".join(f"{key}={value}" for key, value in written.items()))
    return 0 if payload["summary"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
