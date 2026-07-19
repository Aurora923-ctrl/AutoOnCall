"""Run a provenance-bearing benchmark against the real RAG runtime stack."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import config
from app.core.milvus_client import milvus_manager
from app.services.rag_agent_service import RagAgentService
from app.services.rag_retrieval_service import retrieve_structured_knowledge
from scripts.eval.eval_environment import collect_eval_environment

DEFAULT_CASES = REPO_ROOT / "eval" / "rag_holdout_cases.yaml"
DEFAULT_JSON = REPO_ROOT / "logs" / "rag_runtime_benchmark.json"
DEFAULT_MD = REPO_ROOT / "logs" / "rag_runtime_benchmark.md"
DEFAULT_FAILED = REPO_ROOT / "logs" / "rag_runtime_failed_cases.json"


def load_benchmark_cases(path: str | Path, limit: int = 0) -> list[dict[str, Any]]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    normalized = []
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict) or not str(case.get("query") or "").strip():
            continue
        normalized.append(
            {
                "id": str(case.get("id") or f"case-{index}"),
                "query": str(case["query"]).strip(),
                "should_reject": bool(case.get("should_reject")),
                "expected_sources": _string_list(
                    case.get("required_sources")
                    or case.get("expected_sources")
                    or case.get("expected_source")
                ),
                "expected_context_ids": _expected_context_ids(case),
            }
        )
    if limit <= 0:
        selected = normalized
    else:
        selected = normalized[:limit]
    if not selected:
        raise ValueError(f"No runtime RAG benchmark cases found in {path}")
    for case in selected:
        if not case["should_reject"] and not case["expected_sources"]:
            raise ValueError(f"Positive benchmark case {case['id']} lacks expected sources")
    return selected


def _expected_context_ids(case: dict[str, Any]) -> list[str]:
    """Normalize explicit chunk labels without confusing them with file labels."""
    raw = case.get("reference_context_ids") or case.get("approved_chunk_ids")
    if not raw and isinstance(case.get("relevant_chunks"), list):
        raw = [
            item.get("chunk_id") or item.get("id") if isinstance(item, dict) else item
            for item in case["relevant_chunks"]
        ]
    return list(dict.fromkeys(value.strip() for value in _string_list(raw) if value.strip()))


async def run_benchmark(
    cases_path: str | Path,
    *,
    limit: int = 0,
    generate_limit: int = 0,
) -> dict[str, Any]:
    cases_file = Path(cases_path)
    cases = load_benchmark_cases(cases_file, limit)
    run_id = f"rag-runtime-{uuid4().hex}"
    identity = runtime_index_identity(cases)
    indexed_sources = set(identity["indexed_sources"])
    effective_generate_limit = max(0, generate_limit)
    generated_case_ids = select_generated_case_ids(cases, effective_generate_limit)
    agent = RagAgentService(streaming=False) if effective_generate_limit > 0 else None
    results = []
    if identity["status"] != "passed":
        return {
            "schema_version": 1,
            "run": {
                "started_at": datetime.now(UTC).isoformat(),
                "cases_path": str(cases_file),
                "case_set_sha256": hashlib.sha256(cases_file.read_bytes()).hexdigest(),
                "sample_count": 0,
                "dataset_case_count": len(load_benchmark_cases(cases_file, 0)),
                "case_limit": max(0, int(limit)),
                "case_selection": "all_cases" if limit <= 0 else "yaml_prefix",
                "generated_sample_count": 0,
                "generated_case_ids": [],
                "generation_selection": "stable_sha256_case_id",
                "run_id": run_id,
                "indexed_sources": sorted(indexed_sources),
                "index_identity": identity,
            },
            "summary": {
                "status": "invalid_input",
                "passed_count": 0,
                "case_count": 0,
                "result_scope": "dataset_to_index_identity_gate",
                "retrieval": {
                    "status": "invalid_input",
                    "passed_count": 0,
                    "case_count": 0,
                    "failed_cases": [],
                },
                "generation": {
                    "status": "not_run",
                    "passed_count": 0,
                    "case_count": 0,
                    "failed_cases": [],
                },
                "failed_cases": [],
            },
            "cases": [],
        }
    for case in cases:
        generated = agent is not None and case["id"] in generated_case_ids
        try:
            if generated:
                response = await agent.query_with_retrieval(
                    case["query"], f"rag-runtime-benchmark-{case['id']}"
                )
                retrieval = response.get("retrieval", {})
                observability = response.get("observability", {})
                no_answer = bool(response.get("no_answer"))
                citations = response.get("citations", [])
                answer_policy = str(response.get("answer_policy") or "")
                answer = str(response.get("answer") or "")
            else:
                payload = await asyncio.to_thread(retrieve_structured_knowledge, case["query"])
                retrieval = payload
                observability = payload.get("observability", {})
                no_answer = payload.get("status") != "success"
                citations = []
                answer_policy = str(payload.get("answer_policy") or "")
                answer = ""
        except Exception as exc:
            results.append(
                {
                    **case,
                    "generated": generated,
                    "passed": False,
                    "retrieval_passed": False,
                    "generation_passed": False if generated else None,
                    "no_answer": False,
                    "retrieved_sources": [],
                    "retrieved_context_ids": [],
                    "reference_context_ids": case.get("expected_context_ids", []),
                    "observability": {},
                    "failure_reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        retrieved_sources = [
            str(item.get("source_file") or "")
            for item in retrieval.get("retrieval_results", [])
            if isinstance(item, dict)
        ]
        retrieved_context_ids = [
            str(item.get("chunk_id") or "").strip()
            for item in retrieval.get("retrieval_results", [])
            if isinstance(item, dict) and str(item.get("chunk_id") or "").strip()
        ]
        expected_sources = set(case["expected_sources"])
        expected_context_ids = set(case.get("expected_context_ids") or [])
        expected_hit = (
            expected_context_ids.issubset(retrieved_context_ids)
            if expected_context_ids
            else bool(expected_sources) and expected_sources.issubset(retrieved_sources)
        )
        if case["should_reject"]:
            retrieval_passed = no_answer
            generation_passed = (
                no_answer and not citations and answer_policy == "refuse_without_trusted_source"
                if generated
                else None
            )
        else:
            retrieval_passed = expected_hit
            generation_passed = (
                (
                    not no_answer
                    and bool(answer.strip())
                    and answer_policy == "answer_with_citations"
                    and _has_valid_citation(
                        citations,
                        retrieved_sources,
                        required_sources=case["expected_sources"],
                    )
                )
                if generated
                else None
            )
        end_to_end_passed = bool(generated and retrieval_passed and generation_passed)
        results.append(
            {
                **case,
                "generated": generated,
                "passed": end_to_end_passed,
                "retrieval_passed": retrieval_passed,
                "generation_passed": generation_passed,
                "no_answer": no_answer,
                "retrieved_sources": retrieved_sources,
                "retrieved_context_ids": retrieved_context_ids,
                "reference_context_ids": case.get("expected_context_ids", []),
                "observability": observability,
                "answer_policy": answer_policy,
                "citations": citations,
                "failure_reason": (
                    ""
                    if end_to_end_passed
                    else "generation not run"
                    if not generated and retrieval_passed
                    else "runtime retrieval/generation contract failed"
                ),
            }
        )
    return {
        "schema_version": 1,
        "run": {
            "started_at": datetime.now(UTC).isoformat(),
            "cases_path": str(cases_file),
            "case_set_sha256": hashlib.sha256(cases_file.read_bytes()).hexdigest(),
            "sample_count": len(results),
            "dataset_case_count": len(load_benchmark_cases(cases_file, 0)),
            "case_limit": max(0, int(limit)),
            "case_selection": "all_cases" if limit <= 0 else "yaml_prefix",
            "selected_case_ids": [case["id"] for case in cases],
            "generated_sample_count": len(generated_case_ids),
            "generated_case_ids": sorted(generated_case_ids),
            "generation_selection": "stable_sha256_case_id",
            "environment": collect_eval_environment(
                suite="rag_runtime",
                evidence_level="local_live",
                run_id=run_id,
                execution_identity=runtime_execution_identity(results),
            ),
            "run_id": run_id,
            "models": {
                "embedding": config.dashscope_embedding_model,
                "llm": config.effective_rag_model,
                "reranker": "rule-weighted" if config.rag_rerank_enabled else "disabled",
            },
            "indexed_sources": sorted(indexed_sources),
            "index_identity": identity,
        },
        "summary": build_summary(results),
        "cases": results,
    }


def runtime_indexed_sources() -> set[str]:
    """Read source coverage from the active Milvus collection."""
    _ = milvus_manager.connect()
    rows = milvus_manager.get_collection().query(
        expr='id != ""',
        output_fields=["metadata"],
        limit=5000,
    )
    return {
        str(metadata.get("_file_name") or "")
        for row in rows
        if isinstance(metadata := row.get("metadata"), dict) and metadata.get("_file_name")
    }


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    stage_names = [
        "vector_search_ms",
        "lexical_search_ms",
        "fusion_rerank_ms",
        "retrieval_total_ms",
        "llm_generation_ms",
        "total_ms",
    ]
    stages = {}
    for stage in stage_names:
        values = []
        for item in results:
            value = item.get("observability", {}).get("stages", {}).get(stage)
            if isinstance(value, int | float):
                values.append(float(value))
        stages[stage] = distribution(values)
    retrieval_results = [item for item in results if item.get("retrieval_passed") is not None]
    generated_results = [item for item in results if item.get("generated") is True]
    retrieval_passed_count = sum(bool(item.get("retrieval_passed")) for item in retrieval_results)
    generation_passed_count = sum(bool(item.get("generation_passed")) for item in generated_results)
    retrieval_status = (
        "passed"
        if retrieval_results and retrieval_passed_count == len(retrieval_results)
        else "failed"
    )
    generation_status = (
        "not_run"
        if not generated_results
        else "passed"
        if generation_passed_count == len(generated_results)
        else "failed"
    )
    status = (
        "failed"
        if retrieval_status == "failed" or generation_status == "failed"
        else "passed"
        if generation_status == "passed"
        else "retrieval_only_passed"
    )
    return {
        "status": status,
        "passed_count": sum(bool(item["passed"]) for item in results),
        "case_count": len(results),
        "result_scope": "end_to_end_generated_cases_only",
        "retrieval": {
            "status": retrieval_status,
            "passed_count": retrieval_passed_count,
            "case_count": len(retrieval_results),
            "failed_cases": [
                item["id"] for item in retrieval_results if not item.get("retrieval_passed")
            ],
        },
        "generation": {
            "status": generation_status,
            "passed_count": generation_passed_count,
            "case_count": len(generated_results),
            "scope": "selected_product_end_to_end_cases_including_refusals",
            "failed_cases": [
                item["id"] for item in generated_results if not item.get("generation_passed")
            ],
        },
        "stage_latency_ms": stages,
        "token_usage_status": (
            "observed"
            if any(
                item.get("observability", {}).get("token_usage", {}).get("status") == "observed"
                for item in results
            )
            else "not_observed"
        ),
        "failed_cases": [
            item["id"]
            for item in generated_results
            if not item.get("generation_passed") or not item.get("retrieval_passed")
        ],
    }


def runtime_index_identity(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify dataset source/chunk identities against active Milvus rows."""
    _ = milvus_manager.connect()
    rows = milvus_manager.get_collection().query(
        expr='id != ""',
        output_fields=["metadata"],
        limit=5000,
    )
    indexed: set[tuple[str, str]] = set()
    indexed_sources: set[str] = set()
    missing_metadata = 0
    for row in rows:
        metadata = row.get("metadata") if isinstance(row, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        source = str(metadata.get("_file_name") or metadata.get("source_file") or "").strip()
        chunk = str(metadata.get("_chunk_id") or metadata.get("chunk_id") or "").strip()
        if not source or not chunk:
            missing_metadata += 1
            continue
        indexed.add((source, chunk))
        indexed_sources.add(source)
    missing_sources: dict[str, list[str]] = {}
    missing_chunks: dict[str, list[str]] = {}
    for case in cases:
        case_id = str(case.get("id") or "")
        absent_sources = sorted(set(case.get("expected_sources") or []) - indexed_sources)
        if absent_sources:
            missing_sources[case_id] = absent_sources
        absent_chunks = sorted(
            chunk
            for chunk in case.get("expected_context_ids") or []
            if "#" in chunk and (chunk.split("#", 1)[0], chunk) not in indexed
        )
        if absent_chunks:
            missing_chunks[case_id] = absent_chunks
    return {
        "status": (
            "passed"
            if not missing_sources and not missing_chunks and not missing_metadata
            else "failed"
        ),
        "indexed_sources": sorted(indexed_sources),
        "indexed_chunk_count": len(indexed),
        "rows_missing_source_or_chunk_id": missing_metadata,
        "missing_sources_by_case": missing_sources,
        "missing_chunks_by_case": missing_chunks,
    }


def runtime_execution_identity(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Report models observed in runtime payloads instead of only configured names."""
    model_calls = []
    llm_models: set[str] = set()
    embedding_models: set[str] = set()
    for item in results:
        observability = item.get("observability")
        observability = observability if isinstance(observability, dict) else {}
        runtime = observability.get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        llm_model = str(runtime.get("llm_model") or "").strip()
        embedding_model = str(runtime.get("embedding_model") or "").strip()
        if llm_model:
            llm_models.add(llm_model)
        if embedding_model:
            embedding_models.add(embedding_model)
        if llm_model or embedding_model:
            model_calls.append(
                {
                    "case_id": str(item.get("id") or ""),
                    "model": llm_model,
                    "embedding_model": embedding_model,
                    "status": "observed",
                }
            )
    return {
        "actual_model": _single_or_mixed(llm_models, missing="not_run"),
        "actual_embedding_model": _single_or_mixed(
            embedding_models,
            missing="not_observed",
        ),
        "provider": "runtime_api_payload",
        "execution_path": (
            "runtime_retrieval_and_generation"
            if any(item.get("generated") is True for item in results)
            else "runtime_retrieval_only"
        ),
        "fallback_used": False,
        "model_calls": model_calls,
    }


def _single_or_mixed(values: set[str], *, missing: str) -> str:
    if not values:
        return missing
    if len(values) == 1:
        return next(iter(values))
    return "mixed:" + ",".join(sorted(values))


def distribution(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None}
    p95_index = min(len(ordered) - 1, max(0, math_ceil(0.95 * len(ordered)) - 1))
    return {
        "count": len(ordered),
        "p50": round(statistics.median(ordered), 2),
        "p95": round(ordered[p95_index], 2),
    }


def math_ceil(value: float) -> int:
    integer = int(value)
    return integer if value == integer else integer + 1


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    run = payload["run"]
    sample_count = int(run.get("sample_count", summary.get("case_count", 0)) or 0)
    dataset_case_count = int(run.get("dataset_case_count", sample_count) or 0)
    lines = [
        "# RAG Runtime Benchmark",
        "",
        f"- Status: `{summary['status']}`",
        (
            "- End-to-end cases: "
            f"`{summary['generation']['passed_count']}/{summary['generation']['case_count']}`"
        ),
        (
            f"- Dataset coverage: `{sample_count}/{dataset_case_count}`; "
            f"selection `{run.get('case_selection', 'not_reported')}`"
        ),
        (
            f"- Retrieval: `{summary['retrieval']['passed_count']}/"
            f"{summary['retrieval']['case_count']}`; status `{summary['retrieval']['status']}`"
        ),
        (
            f"- Generation: `{summary['generation']['passed_count']}/"
            f"{summary['generation']['case_count']}`; status `{summary['generation']['status']}`"
        ),
        f"- Token usage: `{summary['token_usage_status']}`",
        f"- Case set SHA256: `{run['case_set_sha256']}`",
        "",
        "| Stage | Samples | P50 ms | P95 ms |",
        "| --- | ---: | ---: | ---: |",
    ]
    for stage, metric in summary["stage_latency_ms"].items():
        lines.append(f"| {stage} | {metric['count']} | {metric['p50']} | {metric['p95']} |")
    lines.extend(
        [
            "",
            "> `vector_search_ms` includes query embedding plus Milvus search because the current "
            "LangChain vector-store API does not expose those timings separately.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_artifacts(
    payload: dict[str, Any],
    *,
    json_path: str | Path,
    markdown_path: str | Path,
    failed_path: str | Path,
) -> None:
    for path in (Path(json_path), Path(markdown_path), Path(failed_path)):
        path.parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(markdown_path).write_text(render_markdown(payload), encoding="utf-8")
    Path(failed_path).write_text(
        json.dumps(
            {
                "run": payload["run"],
                "retrieval_failed_cases": [
                    item for item in payload["cases"] if not item.get("retrieval_passed")
                ],
                "generation_failed_cases": [
                    item
                    for item in payload["cases"]
                    if item.get("generated") is True
                    and (not item.get("retrieval_passed") or not item.get("generation_passed"))
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value]
    return [str(value)]


def select_generated_case_ids(cases: list[dict[str, Any]], generate_limit: int) -> set[str]:
    """Select a stable generated subset independent of YAML ordering."""
    limit = max(0, min(int(generate_limit), len(cases)))
    ranked = sorted(
        cases,
        key=lambda case: (
            hashlib.sha256(str(case.get("id") or "").encode("utf-8")).hexdigest(),
            str(case.get("id") or ""),
        ),
    )
    return {str(case["id"]) for case in ranked[:limit]}


def _has_valid_citation(
    citations: Any,
    retrieved_sources: list[str],
    *,
    required_sources: list[str] | None = None,
) -> bool:
    if not isinstance(citations, list):
        return False
    retrieved = set(retrieved_sources)
    cited = {
        str(item.get("source_file") or "")
        for item in citations
        if isinstance(item, dict)
        and str(item.get("source_file") or "") in retrieved
        and bool(str(item.get("chunk_id") or "").strip())
    }
    required = set(required_sources or [])
    return bool(required) and required.issubset(cited)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum YAML-prefix cases to run; 0 runs the complete dataset.",
    )
    parser.add_argument("--generate-limit", type=int, default=0)
    parser.add_argument("--summary-json", default=str(DEFAULT_JSON))
    parser.add_argument("--summary-md", default=str(DEFAULT_MD))
    parser.add_argument("--failed-cases-json", default=str(DEFAULT_FAILED))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = asyncio.run(
        run_benchmark(args.cases, limit=args.limit, generate_limit=args.generate_limit)
    )
    write_artifacts(
        payload,
        json_path=args.summary_json,
        markdown_path=args.summary_md,
        failed_path=args.failed_cases_json,
    )
    print(render_markdown(payload))
    return 0 if payload["summary"]["status"] in {"passed", "retrieval_only_passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
