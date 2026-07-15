"""Verify frozen RAG demo cases across non-streaming and streaming runtime paths."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.rag_agent_service import RagAgentService
from scripts.eval.eval_environment import collect_eval_environment

DEFAULT_CASES = REPO_ROOT / "eval" / "rag_demo_frozen_cases_20260713.yaml"
DEFAULT_JSON = REPO_ROOT / "logs" / "rag_demo_chain_verification_candidate.json"
DEFAULT_MD = REPO_ROOT / "logs" / "rag_demo_chain_verification_candidate.md"


def load_cases(path: Path) -> list[dict[str, Any]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not cases:
        raise ValueError(f"No frozen demo cases found in {path}")
    return [dict(case) for case in cases]


async def verify_case(case: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case["id"])
    expected_policy = str(case.get("acceptance", {}).get("answer_policy") or "")
    should_reject = bool(case.get("should_reject"))
    required_sources = {str(item) for item in case.get("required_sources", [])}
    approved_chunks = {str(item) for item in case.get("approved_chunk_ids", [])}

    nonstream_service = RagAgentService(streaming=False)
    nonstream_started = time.perf_counter()
    nonstream = await nonstream_service.query_with_retrieval(
        str(case["query"]),
        f"demo-verify-nonstream-{case_id}",
    )
    nonstream_elapsed_ms = round((time.perf_counter() - nonstream_started) * 1000, 2)

    stream_service = RagAgentService(streaming=True)
    stream_started = time.perf_counter()
    events = [
        event
        async for event in stream_service.query_stream_with_retrieval(
            str(case["query"]),
            f"demo-verify-stream-{case_id}",
        )
    ]
    stream_elapsed_ms = round((time.perf_counter() - stream_started) * 1000, 2)
    complete_events = [event for event in events if event.get("type") == "complete"]
    stream = complete_events[-1].get("data", {}) if complete_events else {}

    failures = []
    for path_name, result in (("nonstream", nonstream), ("stream", stream)):
        if bool(result.get("no_answer")) != should_reject:
            failures.append(f"{path_name}: no_answer mismatch")
        if str(result.get("answer_policy") or "") != expected_policy:
            failures.append(f"{path_name}: answer_policy mismatch")
        citations = result.get("citations") or []
        if should_reject and citations:
            failures.append(f"{path_name}: refusal citations must be empty")
        if not should_reject and not citations:
            failures.append(f"{path_name}: success citations must be non-empty")
        if not should_reject:
            cited_sources = {
                str(item.get("source_file") or "") for item in citations if isinstance(item, dict)
            }
            if not required_sources.issubset(cited_sources):
                failures.append(f"{path_name}: required sources missing")
            cited_chunks = {
                str(item.get("chunk_id") or "") for item in citations if isinstance(item, dict)
            }
            if not cited_chunks.issubset(approved_chunks):
                failures.append(f"{path_name}: unapproved citation chunk")

    if bool(nonstream.get("no_answer")) != bool(stream.get("no_answer")):
        failures.append("stream/nonstream no_answer mismatch")
    if str(nonstream.get("answer_policy") or "") != str(stream.get("answer_policy") or ""):
        failures.append("stream/nonstream answer_policy mismatch")

    return {
        "id": case_id,
        "query": case["query"],
        "passed": not failures,
        "failures": failures,
        "nonstream": compact_result(nonstream, nonstream_elapsed_ms),
        "stream": {
            **compact_result(stream, stream_elapsed_ms),
            "event_types": [str(event.get("type") or "") for event in events],
            "content_event_count": sum(event.get("type") == "content" for event in events),
        },
    }


def compact_result(result: dict[str, Any], elapsed_ms: float) -> dict[str, Any]:
    citations = result.get("citations") or []
    return {
        "elapsed_ms": elapsed_ms,
        "answer": str(result.get("answer") or ""),
        "no_answer": bool(result.get("no_answer")),
        "answer_policy": str(result.get("answer_policy") or ""),
        "citations": [
            {
                "source_file": str(item.get("source_file") or ""),
                "chunk_id": str(item.get("chunk_id") or ""),
            }
            for item in citations
            if isinstance(item, dict)
        ],
        "observability": result.get("observability") or {},
    }


async def run(cases_path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    cases = load_cases(cases_path)
    results = [await verify_case(case) for case in cases]
    elapsed_seconds = round(time.perf_counter() - started, 2)
    return {
        "schema_version": 1,
        "run": {
            "started_at": datetime.now(UTC).isoformat(),
            "cases_path": str(cases_path),
            "case_set_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
            "environment": collect_eval_environment(suite="rag_demo_chain"),
        },
        "summary": {
            "status": "passed" if all(item["passed"] for item in results) else "failed",
            "passed_count": sum(item["passed"] for item in results),
            "case_count": len(results),
            "elapsed_seconds": elapsed_seconds,
            "within_five_minutes": elapsed_seconds <= 300,
            "failed_cases": [item["id"] for item in results if not item["passed"]],
        },
        "cases": results,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Frozen RAG Demo Chain Verification",
        "",
        f"- Status: `{summary['status']}`",
        f"- Cases: `{summary['passed_count']}/{summary['case_count']}`",
        f"- Stream + non-stream elapsed: `{summary['elapsed_seconds']}s`",
        f"- Within five minutes: `{summary['within_five_minutes']}`",
        f"- Case set SHA256: `{payload['run']['case_set_sha256']}`",
        "",
        "| Case | Status | Non-stream ms | Stream ms | Policy |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for case in payload["cases"]:
        lines.append(
            f"| `{case['id']}` | `{'PASS' if case['passed'] else 'FAIL'}` | "
            f"{case['nonstream']['elapsed_ms']} | {case['stream']['elapsed_ms']} | "
            f"`{case['nonstream']['answer_policy']}` |"
        )
        for failure in case["failures"]:
            lines.append(f"|  |  |  |  | Failure: {failure} |")
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--summary-json", default=str(DEFAULT_JSON))
    parser.add_argument("--summary-md", default=str(DEFAULT_MD))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = asyncio.run(run(Path(args.cases)))
    Path(args.summary_json).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    Path(args.summary_md).write_text(render_markdown(payload), encoding="utf-8")
    print(render_markdown(payload))
    return 0 if payload["summary"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
