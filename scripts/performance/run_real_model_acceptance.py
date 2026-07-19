"""Run the bounded stage-6 real-model acceptance workload."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.eval.eval_environment import collect_eval_environment

DEFAULT_RAG_REQUESTS = 30
DEFAULT_AIOPS_REQUESTS = 20
DEFAULT_RAG_CASES_PATH = Path("eval/rag_demo_frozen_cases_20260713.yaml")
ACCEPTED_AIOPS_TERMINAL_STATUSES = {"completed", "degraded"}


async def run_acceptance(
    *,
    base_url: str,
    rag_requests: int,
    aiops_requests: int,
    concurrency: int,
    rag_cases_path: str | Path = DEFAULT_RAG_CASES_PATH,
) -> dict[str, Any]:
    """Execute real RAG and AIOps requests and retain request-level evidence."""
    if rag_requests < 0 or aiops_requests < 0:
        raise ValueError("request counts must be non-negative")
    run_id = f"stage6-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    semaphore = asyncio.Semaphore(max(1, concurrency))
    timeout = httpx.Timeout(240.0)
    limits = httpx.Limits(max_connections=max(2, concurrency))
    rag_cases = load_rag_acceptance_cases(rag_cases_path) if rag_requests else []
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        tasks = [
            _bounded(
                semaphore,
                _run_rag(
                    client,
                    base_url=base_url,
                    run_id=run_id,
                    index=index,
                    case=rag_cases[index % len(rag_cases)],
                ),
            )
            for index in range(rag_requests)
        ]
        tasks.extend(
            _bounded(
                semaphore,
                _run_aiops(client, base_url=base_url, run_id=run_id, index=index),
            )
            for index in range(aiops_requests)
        )
        requests = await asyncio.gather(*tasks)

    rag = [item for item in requests if item["request_kind"] == "rag"]
    aiops = [item for item in requests if item["request_kind"] == "aiops"]
    failures = [item for item in requests if not item["passed"]]
    requested_rag_case_ids = {
        str(item.get("details", {}).get("case_id") or "")
        for item in rag
        if str(item.get("details", {}).get("case_id") or "")
    }
    dataset_rag_case_ids = {str(case["id"]) for case in rag_cases}
    rag_case_coverage_met = (
        not dataset_rag_case_ids or requested_rag_case_ids == dataset_rag_case_ids
    )
    status = acceptance_run_status(
        requests,
        rag_requests=rag_requests,
        aiops_requests=aiops_requests,
        semantic_claims_unverified=any(
            case["required_claims"] or case["forbidden_claims"] for case in rag_cases
        ),
        rag_case_coverage_met=rag_case_coverage_met,
    )
    semantic_claim_case_ids = [
        case["id"] for case in rag_cases if case["required_claims"] or case["forbidden_claims"]
    ]
    return {
        "run": {
            "run_id": run_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "base_url": base_url,
            "evidence_level": "local_live",
            "model_requests_enabled": True,
            "concurrency": max(1, concurrency),
            "rag_cases_path": str(Path(rag_cases_path)),
            "rag_case_set_sha256": (
                hashlib.sha256(Path(rag_cases_path).read_bytes()).hexdigest()
                if rag_requests
                else ""
            ),
            "rag_case_ids": [case["id"] for case in rag_cases],
            "environment": collect_eval_environment(
                suite="real_model_acceptance",
                evidence_level="local_live",
                run_id=run_id,
                execution_identity=acceptance_execution_identity(requests),
            ),
        },
        "summary": {
            "status": status,
            "request_count": len(requests),
            "passed_count": len(requests) - len(failures),
            "failure_count": len(failures),
            "rag": _request_summary(rag, required=rag_requests),
            "aiops": _request_summary(aiops, required=aiops_requests),
            "rag_case_coverage": {
                "status": "met" if rag_case_coverage_met else "not_met",
                "dataset_case_count": len(dataset_rag_case_ids),
                "observed_case_count": len(requested_rag_case_ids),
                "observed_case_ids": sorted(requested_rag_case_ids),
                "missing_case_ids": sorted(dataset_rag_case_ids - requested_rag_case_ids),
            },
            "semantic_claim_validation": {
                "status": "not_run" if semantic_claim_case_ids else "not_required",
                "case_ids": semantic_claim_case_ids,
                "reason": (
                    "Required and forbidden natural-language claims need a bound Judge or "
                    "human review. Citation and approved-chunk checks do not prove entailment."
                    if semantic_claim_case_ids
                    else ""
                ),
            },
            "token_usage": {
                "status": "not_observed",
                "reason": (
                    "The current HTTP response and persisted Trace contract do not expose provider "
                    "token usage. No token amount or monetary cost is fabricated."
                ),
            },
            "cost": {
                "status": "not_run",
                "reason": "No dated, source-attributed price snapshot was applied.",
            },
        },
        "requests": requests,
    }


async def _bounded(semaphore: asyncio.Semaphore, awaitable: Any) -> dict[str, Any]:
    async with semaphore:
        return await awaitable


async def _run_rag(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    run_id: str,
    index: int,
    case: dict[str, Any],
) -> dict[str, Any]:
    request_id = f"{run_id}-rag-{index:02d}-{uuid4().hex[:6]}"
    started = time.perf_counter()
    try:
        response = await client.post(
            f"{base_url}/api/chat",
            json={
                "Id": request_id,
                "Question": case["query"],
                "EvidenceLevel": "local_live",
                "AcceptanceRunId": run_id,
            },
        )
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else {}
        data = data if isinstance(data, dict) else {}
        validation_errors = validate_rag_response(data, case)
        success = (
            response.status_code == 200 and bool(data.get("success")) and not validation_errors
        )
        return _request_result(
            request_id=request_id,
            request_kind="rag",
            status_code=response.status_code,
            passed=success,
            started=started,
            error=(
                ""
                if success
                else "; ".join(validation_errors)
                or str(payload.get("message") or "RAG request failed")
            ),
            details={
                "case_id": case["id"],
                "answer_policy": str(data.get("answerPolicy") or ""),
                "no_answer": bool(data.get("noAnswer")),
                "cited_sources": sorted(
                    {
                        str(item.get("source_file") or "")
                        for item in data.get("citations", [])
                        if isinstance(item, dict) and str(item.get("source_file") or "")
                    }
                ),
                "retrieval_status": str((data.get("retrieval") or {}).get("status") or ""),
                "retrieval_mode": str((data.get("retrieval") or {}).get("retrieval_mode") or ""),
                "runtime_model": str(
                    (data.get("observability") or {}).get("runtime", {}).get("llm_model") or ""
                ),
            },
        )
    except Exception as exc:
        return _request_result(
            request_id=request_id,
            request_kind="rag",
            status_code=0,
            passed=False,
            started=started,
            error=f"{type(exc).__name__}: {exc}",
            details={"case_id": case["id"]},
        )


async def _run_aiops(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    run_id: str,
    index: int,
) -> dict[str, Any]:
    request_id = f"{run_id}-aiops-{index:02d}-{uuid4().hex[:6]}"
    incident_id = f"INC-{run_id.upper()}-{index:02d}"
    body = {
        "session_id": request_id,
        "incident": {
            "incident_id": incident_id,
            "title": "order-service Redis saturation",
            "service_name": "order-service",
            "severity": "P2",
            "symptom": "Redis connection timeout and elevated 5xx",
            "environment": "local-live",
            "raw_alert": {
                "evidence_level": "local_live",
                "acceptance_run_id": run_id,
                "alertname": "RedisMaxclientsNearLimit",
                "dependency": "redis",
                "redis_instance": "redis-cluster-prod",
            },
        },
    }
    started = time.perf_counter()
    completed = False
    terminal_status = ""
    degradation_analysis: dict[str, Any] = {}
    validation_error = ""
    status_code = 0
    try:
        async with client.stream(
            "POST",
            f"{base_url}/api/aiops",
            content=json.dumps(body),
            headers={"Content-Type": "application/json"},
        ) as response:
            status_code = response.status_code
            async for line in response.aiter_lines():
                event = _parse_sse_event(line)
                if event.get("type") == "error":
                    validation_error = str(event.get("message") or "AIOps stream emitted error")
                    break
                if event.get("type") == "complete":
                    completed = True
                    terminal_status = str(event.get("status") or "")
                    structured_report = event.get("structured_report")
                    structured_report = (
                        structured_report if isinstance(structured_report, dict) else {}
                    )
                    degradation_analysis = event.get(
                        "degradation_analysis"
                    ) or structured_report.get("degradation_analysis")
                    degradation_analysis = (
                        degradation_analysis if isinstance(degradation_analysis, dict) else {}
                    )
                    break
        degraded_is_safe = (
            terminal_status != "degraded" or degradation_analysis.get("safe_terminal") is True
        )
        passed = (
            status_code == 200
            and completed
            and terminal_status in ACCEPTED_AIOPS_TERMINAL_STATUSES
            and degraded_is_safe
        )
        return _request_result(
            request_id=request_id,
            request_kind="aiops",
            status_code=status_code,
            passed=passed,
            started=started,
            error=(
                ""
                if passed
                else validation_error
                or (
                    f"unaccepted AIOps terminal status: {terminal_status or 'missing'}"
                    if completed and terminal_status not in ACCEPTED_AIOPS_TERMINAL_STATUSES
                    else "degraded AIOps terminal state is not classified as safe"
                    if completed and terminal_status == "degraded"
                    else "SSE stream ended without a complete event"
                )
            ),
            details={
                "terminal_status": terminal_status,
                "degradation_analysis": degradation_analysis,
                "acceptance_class": (
                    "complete"
                    if terminal_status == "completed"
                    else "safe_degraded"
                    if terminal_status == "degraded" and degraded_is_safe
                    else "unsafe_degraded"
                    if terminal_status == "degraded"
                    else "rejected"
                ),
            },
        )
    except Exception as exc:
        return _request_result(
            request_id=request_id,
            request_kind="aiops",
            status_code=status_code,
            passed=False,
            started=started,
            error=f"{type(exc).__name__}: {exc}",
            details={"terminal_status": terminal_status},
        )


def _request_result(
    *,
    request_id: str,
    request_kind: str,
    status_code: int,
    passed: bool,
    started: float,
    error: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "request_kind": request_kind,
        "evidence_level": "local_live",
        "status_code": status_code,
        "passed": passed,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "error": error,
        "details": details or {},
    }


def _request_summary(requests: list[dict[str, Any]], *, required: int) -> dict[str, Any]:
    passed = [item for item in requests if item["passed"]]
    degraded = [
        item
        for item in passed
        if str((item.get("details") or {}).get("terminal_status") or "") == "degraded"
    ]
    latencies = sorted(float(item["latency_ms"]) for item in requests)
    passed_latencies = sorted(float(item["latency_ms"]) for item in passed)
    return {
        "required": required,
        "observed": len(requests),
        "passed": len(passed),
        "completed": len(passed) - len(degraded),
        "degraded": len(degraded),
        "failed": len(requests) - len(passed),
        "acceptance_status": (
            "not_run" if required == 0 else "met" if len(passed) >= required else "not_met"
        ),
        "latency_ms": {
            "count": len(latencies),
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "min": round(latencies[0], 2) if latencies else 0.0,
            "max": round(latencies[-1], 2) if latencies else 0.0,
        },
        "accepted_latency_ms": {
            "count": len(passed_latencies),
            "p50": _percentile(passed_latencies, 0.50),
            "p95": _percentile(passed_latencies, 0.95),
            "min": round(passed_latencies[0], 2) if passed_latencies else 0.0,
            "max": round(passed_latencies[-1], 2) if passed_latencies else 0.0,
        },
    }


def acceptance_execution_identity(requests: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind acceptance provenance to models observed in successful HTTP payloads."""
    model_calls = []
    models: set[str] = set()
    for item in requests:
        details = item.get("details")
        details = details if isinstance(details, dict) else {}
        model = str(details.get("runtime_model") or "").strip()
        if not model:
            continue
        models.add(model)
        model_calls.append(
            {
                "request_id": str(item.get("request_id") or ""),
                "request_kind": str(item.get("request_kind") or ""),
                "model": model,
                "status": "observed",
            }
        )
    actual_model = (
        "not_observed"
        if not models
        else next(iter(models))
        if len(models) == 1
        else "mixed:" + ",".join(sorted(models))
    )
    return {
        "actual_model": actual_model,
        "actual_embedding_model": "not_observed",
        "provider": "http_runtime_payload",
        "execution_path": "local_live_http_acceptance",
        "fallback_used": False,
        "model_calls": model_calls,
    }


def acceptance_run_status(
    requests: list[dict[str, Any]],
    *,
    rag_requests: int,
    aiops_requests: int,
    semantic_claims_unverified: bool = False,
    rag_case_coverage_met: bool = True,
) -> str:
    if any(not item["passed"] for item in requests):
        return "failed"
    if not requests:
        return "not_run"
    if rag_requests == 0 or aiops_requests == 0:
        return "incomplete"
    if not rag_case_coverage_met:
        return "incomplete"
    if semantic_claims_unverified:
        return "incomplete"
    if any(
        str((item.get("details") or {}).get("terminal_status") or "") == "degraded"
        for item in requests
    ):
        return "passed_with_degraded"
    return "passed"


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, math.ceil(len(values) * quantile) - 1))
    return round(values[index], 2)


def load_rag_acceptance_cases(path: str | Path) -> list[dict[str, Any]]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    raw_cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not raw_cases and isinstance(payload, dict):
        standby = payload.get("standby_cases")
        raw_cases = standby if isinstance(standby, list) else []
    cases = []
    case_ids: set[str] = set()
    for item in raw_cases:
        if not isinstance(item, dict) or not str(item.get("query") or "").strip():
            continue
        acceptance = item.get("acceptance")
        acceptance = acceptance if isinstance(acceptance, dict) else {}
        case = {
            "id": str(item.get("id") or f"case-{len(cases) + 1}"),
            "query": str(item["query"]).strip(),
            "should_reject": bool(item.get("should_reject")),
            "required_sources": _string_list(item.get("required_sources")),
            "approved_chunk_ids": _string_list(item.get("approved_chunk_ids")),
            "answer_policy": str(acceptance.get("answer_policy") or ""),
            "citations_must_be_empty": bool(acceptance.get("citations_must_be_empty")),
            "required_claims": _string_list(acceptance.get("required_claims")),
            "forbidden_claims": _string_list(acceptance.get("forbidden_claims")),
        }
        if case["id"] in case_ids:
            raise ValueError(f"Duplicate real-model RAG acceptance case id: {case['id']}")
        case_ids.add(case["id"])
        _validate_rag_acceptance_case(case)
        cases.append(case)
    if not cases:
        raise ValueError(f"No real-model RAG acceptance cases found in {path}")
    return cases


def _validate_rag_acceptance_case(case: dict[str, Any]) -> None:
    if case["should_reject"]:
        if case["answer_policy"] != "refuse_without_trusted_source":
            raise ValueError(f"Refusal case {case['id']} lacks the refusal answer policy")
        if not case["citations_must_be_empty"]:
            raise ValueError(f"Refusal case {case['id']} must require empty citations")
        return
    if not case["required_sources"]:
        raise ValueError(f"Positive case {case['id']} lacks required_sources")
    if not case["approved_chunk_ids"]:
        raise ValueError(f"Positive case {case['id']} lacks approved_chunk_ids")
    if case["answer_policy"] != "answer_with_citations":
        raise ValueError(f"Positive case {case['id']} lacks the cited-answer policy")


def validate_rag_response(data: dict[str, Any], case: dict[str, Any]) -> list[str]:
    errors = []
    no_answer = bool(data.get("noAnswer"))
    answer_policy = str(data.get("answerPolicy") or "")
    answer = str(data.get("answer") or "")
    citations = data.get("citations")
    citation_items = citations if isinstance(citations, list) else []
    valid_citations = [
        item
        for item in citation_items
        if isinstance(item, dict)
        and str(item.get("source_file") or "").strip()
        and str(item.get("chunk_id") or "").strip()
    ]
    retrieval = data.get("retrieval")
    retrieval = retrieval if isinstance(retrieval, dict) else {}
    retrieval_mode = str(retrieval.get("retrieval_mode") or "")
    if not retrieval_mode or "offline" in retrieval_mode or "fixture" in retrieval_mode:
        errors.append("retrieval mode does not prove a runtime retrieval")
    if case["should_reject"]:
        if not no_answer:
            errors.append("expected refusal but noAnswer=false")
        if answer_policy != case["answer_policy"]:
            errors.append("refusal answerPolicy mismatch")
        if case["citations_must_be_empty"] and valid_citations:
            errors.append("refusal returned citations")
        return errors

    observability = data.get("observability")
    observability = observability if isinstance(observability, dict) else {}
    runtime = observability.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    if not str(runtime.get("llm_model") or "").strip():
        errors.append("runtime LLM model evidence is missing")
    if no_answer:
        errors.append("positive case was refused")
    if answer_policy != case["answer_policy"]:
        errors.append("positive answerPolicy mismatch")
    if not answer.strip():
        errors.append("answer is empty")
    if retrieval.get("status") != "success":
        errors.append("retrieval status is not success")
    cited_sources = {str(item.get("source_file") or "") for item in valid_citations}
    cited_chunks = {str(item.get("chunk_id") or "") for item in valid_citations}
    mismatched_citations = [
        item
        for item in valid_citations
        if str(item.get("chunk_id") or "").split("#", 1)[0] != str(item.get("source_file") or "")
    ]
    if mismatched_citations:
        errors.append("citation source_file and chunk_id source do not match")
    missing_sources = set(case["required_sources"]) - cited_sources
    if missing_sources:
        errors.append(f"missing required citation sources: {sorted(missing_sources)}")
    approved_chunks = set(case["approved_chunk_ids"])
    missing_approved_sources = [
        source
        for source in case["required_sources"]
        if not any(
            chunk_id in approved_chunks and chunk_id.split("#", 1)[0] == source
            for chunk_id in cited_chunks
        )
    ]
    if missing_approved_sources:
        errors.append(
            f"required sources lack approved cited chunks: {sorted(missing_approved_sources)}"
        )
    return errors


def _parse_sse_event(line: str) -> dict[str, Any]:
    value = str(line or "").strip()
    if not value.startswith("data:"):
        return {}
    try:
        payload = json.loads(value.removeprefix("data:").strip())
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def write_outputs(payload: dict[str, Any], *, json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = payload["summary"]
    lines = [
        "# AutoOnCall Stage-6 Real-Model Acceptance",
        "",
        f"- Run ID: `{payload['run']['run_id']}`",
        f"- Evidence level: `{payload['run']['evidence_level']}`",
        f"- Status: `{summary['status']}`",
        f"- Requests: `{summary['passed_count']}/{summary['request_count']}` passed",
        "",
        "| Workload | Required | Passed | P50 | P95 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for key in ("rag", "aiops"):
        item = summary[key]
        lines.append(
            f"| {key.upper()} | {item['required']} | {item['passed']} | "
            f"{item['latency_ms']['p50']} ms | {item['latency_ms']['p95']} ms |"
        )
    lines.extend(
        [
            "",
            "## Token And Cost Boundary",
            "",
            f"- Token usage: `{summary['token_usage']['status']}`. "
            f"{summary['token_usage']['reason']}",
            f"- Cost: `{summary['cost']['status']}`. {summary['cost']['reason']}",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:9900")
    parser.add_argument("--rag-requests", type=int, default=DEFAULT_RAG_REQUESTS)
    parser.add_argument("--aiops-requests", type=int, default=DEFAULT_AIOPS_REQUESTS)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--rag-cases", default=str(DEFAULT_RAG_CASES_PATH))
    parser.add_argument("--summary-json", default="logs/performance_real_model.json")
    parser.add_argument("--summary-md", default="logs/performance_real_model.md")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = asyncio.run(
        run_acceptance(
            base_url=args.base_url.rstrip("/"),
            rag_requests=max(0, args.rag_requests),
            aiops_requests=max(0, args.aiops_requests),
            concurrency=max(1, args.concurrency),
            rag_cases_path=args.rag_cases,
        )
    )
    write_outputs(
        payload,
        json_path=Path(args.summary_json),
        md_path=Path(args.summary_md),
    )
    print(
        "Stage-6 real-model acceptance: "
        f"{payload['summary']['status']}; "
        f"rag={payload['summary']['rag']['passed']}/{payload['summary']['rag']['required']}; "
        f"aiops={payload['summary']['aiops']['passed']}/"
        f"{payload['summary']['aiops']['required']}"
    )
    return 0 if payload["summary"]["status"] in {"passed", "passed_with_degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
