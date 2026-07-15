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


def load_benchmark_cases(path: str | Path, limit: int) -> list[dict[str, Any]]:
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
            }
        )
    return normalized[: max(limit, 1)]


async def run_benchmark(
    cases_path: str | Path,
    *,
    limit: int = 20,
    generate_limit: int = 0,
) -> dict[str, Any]:
    cases_file = Path(cases_path)
    cases = load_benchmark_cases(cases_file, limit)
    indexed_sources = runtime_indexed_sources()
    agent = RagAgentService(streaming=False) if generate_limit > 0 else None
    results = []
    for index, case in enumerate(cases):
        if agent is not None and index < generate_limit:
            response = await agent.query_with_retrieval(
                case["query"], f"rag-runtime-benchmark-{case['id']}"
            )
            retrieval = response.get("retrieval", {})
            observability = response.get("observability", {})
            no_answer = bool(response.get("no_answer"))
        else:
            payload = await asyncio.to_thread(retrieve_structured_knowledge, case["query"])
            retrieval = payload
            observability = payload.get("observability", {})
            no_answer = payload.get("status") != "success"
        retrieved_sources = [
            str(item.get("source_file") or "")
            for item in retrieval.get("retrieval_results", [])
            if isinstance(item, dict)
        ]
        passed = (
            no_answer
            if case["should_reject"]
            else bool(set(case["expected_sources"]).intersection(retrieved_sources))
        )
        results.append(
            {
                **case,
                "passed": passed,
                "no_answer": no_answer,
                "retrieved_sources": retrieved_sources,
                "observability": observability,
            }
        )
    return {
        "schema_version": 1,
        "run": {
            "started_at": datetime.now(UTC).isoformat(),
            "cases_path": str(cases_file),
            "case_set_sha256": hashlib.sha256(cases_file.read_bytes()).hexdigest(),
            "sample_count": len(results),
            "generated_sample_count": min(generate_limit, len(results)),
            "environment": collect_eval_environment(suite="rag_runtime"),
            "models": {
                "embedding": config.dashscope_embedding_model,
                "llm": config.effective_rag_model,
                "reranker": "rule-weighted" if config.rag_rerank_enabled else "disabled",
            },
            "indexed_sources": sorted(indexed_sources),
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
    return {
        "status": "passed" if results and all(item["passed"] for item in results) else "failed",
        "passed_count": sum(bool(item["passed"]) for item in results),
        "case_count": len(results),
        "stage_latency_ms": stages,
        "token_usage_status": (
            "observed"
            if any(
                item.get("observability", {}).get("token_usage", {}).get("status") == "observed"
                for item in results
            )
            else "not_observed"
        ),
        "failed_cases": [item["id"] for item in results if not item["passed"]],
    }


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
    lines = [
        "# RAG Runtime Benchmark",
        "",
        f"- Status: `{summary['status']}`",
        f"- Cases: `{summary['passed_count']}/{summary['case_count']}`",
        f"- Token usage: `{summary['token_usage_status']}`",
        f"- Case set SHA256: `{payload['run']['case_set_sha256']}`",
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
                "failed_cases": [item for item in payload["cases"] if not item["passed"]],
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--limit", type=int, default=20)
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
    return 0 if payload["summary"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
