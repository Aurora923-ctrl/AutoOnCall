"""Run the bounded stage-6 real-model acceptance workload."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

DEFAULT_RAG_REQUESTS = 20
DEFAULT_AIOPS_REQUESTS = 10


async def run_acceptance(
    *,
    base_url: str,
    rag_requests: int,
    aiops_requests: int,
    concurrency: int,
) -> dict[str, Any]:
    """Execute real RAG and AIOps requests and retain request-level evidence."""
    run_id = f"stage6-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    semaphore = asyncio.Semaphore(max(1, concurrency))
    timeout = httpx.Timeout(240.0)
    limits = httpx.Limits(max_connections=max(2, concurrency))
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        tasks = [
            _bounded(
                semaphore,
                _run_rag(client, base_url=base_url, run_id=run_id, index=index),
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
    return {
        "run": {
            "run_id": run_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "base_url": base_url,
            "evidence_level": "local_live",
            "model_requests_enabled": True,
            "concurrency": max(1, concurrency),
        },
        "summary": {
            "status": "passed" if not failures else "failed",
            "request_count": len(requests),
            "passed_count": len(requests) - len(failures),
            "failure_count": len(failures),
            "rag": _request_summary(rag, required=rag_requests),
            "aiops": _request_summary(aiops, required=aiops_requests),
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
) -> dict[str, Any]:
    request_id = f"{run_id}-rag-{index:02d}-{uuid4().hex[:6]}"
    started = time.perf_counter()
    try:
        response = await client.post(
            f"{base_url}/api/chat",
            json={
                "Id": request_id,
                "Question": (
                    "How should Redis maxclients saturation be diagnosed with evidence, "
                    "approval, dry-run, and rollback boundaries?"
                ),
            },
        )
        payload = response.json()
        success = response.status_code == 200 and bool(payload.get("data", {}).get("success"))
        return _request_result(
            request_id=request_id,
            request_kind="rag",
            status_code=response.status_code,
            passed=success,
            started=started,
            error="" if success else str(payload.get("message") or "RAG request failed"),
        )
    except Exception as exc:
        return _request_result(
            request_id=request_id,
            request_kind="rag",
            status_code=0,
            passed=False,
            started=started,
            error=f"{type(exc).__name__}: {exc}",
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
            },
        },
    }
    started = time.perf_counter()
    completed = False
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
                if '"type":"complete"' in line or '"type": "complete"' in line:
                    completed = True
                    break
        return _request_result(
            request_id=request_id,
            request_kind="aiops",
            status_code=status_code,
            passed=status_code == 200 and completed,
            started=started,
            error="" if completed else "SSE stream ended without a complete event",
        )
    except Exception as exc:
        return _request_result(
            request_id=request_id,
            request_kind="aiops",
            status_code=status_code,
            passed=False,
            started=started,
            error=f"{type(exc).__name__}: {exc}",
        )


def _request_result(
    *,
    request_id: str,
    request_kind: str,
    status_code: int,
    passed: bool,
    started: float,
    error: str,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "request_kind": request_kind,
        "evidence_level": "local_live",
        "status_code": status_code,
        "passed": passed,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "error": error,
    }


def _request_summary(requests: list[dict[str, Any]], *, required: int) -> dict[str, Any]:
    passed = [item for item in requests if item["passed"]]
    latencies = sorted(float(item["latency_ms"]) for item in passed)
    return {
        "required": required,
        "observed": len(requests),
        "passed": len(passed),
        "failed": len(requests) - len(passed),
        "acceptance_status": "met" if len(passed) >= required else "not_met",
        "latency_ms": {
            "count": len(latencies),
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "min": round(latencies[0], 2) if latencies else 0.0,
            "max": round(latencies[-1], 2) if latencies else 0.0,
        },
    }


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, math.ceil(len(values) * quantile) - 1))
    return round(values[index], 2)


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
    return 0 if payload["summary"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
