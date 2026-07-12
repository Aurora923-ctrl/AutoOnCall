"""Aggregate persisted performance traces without overstating evidence or cost."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import config
from app.services.aiops_store import AIOpsStateStore, create_aiops_store
from scripts.eval.eval_environment import (
    EVIDENCE_LEVELS,
    collect_eval_environment,
    provenance_markdown_lines,
)

DEFAULT_OUTPUT_JSON = REPO_ROOT / "logs" / "performance_summary.json"
DEFAULT_OUTPUT_MD = REPO_ROOT / "logs" / "performance_summary.md"
DEFAULT_PRICE_SNAPSHOT = REPO_ROOT / "config" / "llm_price_snapshot.json"
DEFAULT_REQUIRED_RAG_REQUESTS = 30
DEFAULT_REQUIRED_AIOPS_REQUESTS = 20
VALID_PRICE_UNITS = {"usd_per_million_tokens", "cny_per_million_tokens"}
PHASE_ALIASES = {
    "retrieve": "retrieval",
    "retriever": "retrieval",
    "rag_retrieval": "retrieval",
    "planning": "planner",
    "plan": "planner",
    "execute": "executor",
    "execution": "executor",
    "replan": "replanner",
    "report_generator": "report",
}


def evaluate_performance(
    *,
    limit: int = 500,
    evidence_level: str = "offline_fixture",
    price_snapshot_path: Path | None = None,
    required_rag_requests: int = DEFAULT_REQUIRED_RAG_REQUESTS,
    required_aiops_requests: int = DEFAULT_REQUIRED_AIOPS_REQUESTS,
    store: AIOpsStateStore | None = None,
) -> dict[str, Any]:
    """Read persisted trace events and report only observations supported by them."""
    supported_levels = EVIDENCE_LEVELS | {"unclassified"}
    if evidence_level not in supported_levels:
        raise ValueError(
            f"Unsupported evidence_level={evidence_level}; supported={sorted(supported_levels)}"
        )

    active_store = store or create_aiops_store()
    events = active_store.list_trace_events()
    selected_events = events[-max(1, limit) :]
    samples = [_event_sample(event, evidence_level=evidence_level) for event in selected_events]
    samples = [sample for sample in samples if sample is not None]

    request_samples = build_request_samples(samples)
    request_counts = Counter(
        sample["request_kind"] for sample in request_samples if sample["request_kind"] != "unknown"
    )
    observed_levels = sorted(
        {
            sample["evidence_level"]
            for sample in samples
            if sample["evidence_level"] in EVIDENCE_LEVELS
        }
    )
    evidence_conflicts = [
        sample for sample in samples if sample["evidence_level"] != evidence_level
    ]
    coverage = {
        "rag": _coverage_entry(request_counts["rag"], required_rag_requests),
        "aiops": _coverage_entry(request_counts["aiops"], required_aiops_requests),
    }
    real_request_gate = (
        evidence_level in {"local_live", "controlled_fault", "production"}
        and not evidence_conflicts
        and all(item["status"] == "met" for item in coverage.values())
    )

    usage = aggregate_token_usage(samples)
    price_snapshot = load_price_snapshot(price_snapshot_path)
    cost = calculate_cost(samples, price_snapshot)
    status = _evaluation_status(
        samples=samples,
        evidence_conflicts=evidence_conflicts,
        real_request_gate=real_request_gate,
    )
    return {
        "run": {
            "generated_at": datetime.now(UTC).isoformat(),
            "evidence_level": evidence_level,
            "observed_evidence_levels": observed_levels,
            "scope": (
                "persisted request, phase, tool, latency, retry, timeout, token, and optional "
                "dated price observations"
            ),
            "storage_backend": config.aiops_storage_backend,
            "environment": collect_eval_environment(
                suite="performance",
                evidence_level=evidence_level,
            ),
        },
        "summary": {
            "status": status,
            "reason": _status_reason(
                status=status,
                evidence_level=evidence_level,
                samples=samples,
                evidence_conflicts=evidence_conflicts,
                coverage=coverage,
            ),
            "event_sample_count": len(samples),
            "request_sample_count": len(request_samples),
            "request_counts": dict(sorted(request_counts.items())),
            "required_real_request_coverage": coverage,
            "real_request_acceptance_gate": "passed" if real_request_gate else "not_run",
            "latency_ms": distribution([sample["latency_ms"] for sample in request_samples]),
            "first_event_latency_ms": distribution(
                _metadata_numbers(request_samples, "first_event_latency_ms")
            ),
            "time_to_first_token_ms": distribution(
                _metadata_numbers(request_samples, "time_to_first_token_ms")
            ),
            "phase_latency_ms": aggregate_group_latency(samples, "phase"),
            "tool_latency_ms": aggregate_group_latency(samples, "tool_name"),
            "queue_latency_ms": distribution(
                _metadata_numbers(samples, "queue_latency_ms", "concurrency_wait_ms")
            ),
            "llm": aggregate_llm_reliability(samples),
            "token_usage": usage,
            "cost": cost,
            "evidence": {
                "requested_level": evidence_level,
                "observed_levels": observed_levels,
                "conflict_count": len(evidence_conflicts),
                "conflicts": [
                    {
                        "event_id": item["event_id"],
                        "trace_id": item["trace_id"],
                        "observed": item["evidence_level"],
                    }
                    for item in evidence_conflicts[:20]
                ],
            },
        },
        "requests": request_samples,
        "samples": samples,
    }


def aggregate_token_usage(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate OpenAI-compatible usage dictionaries stored in trace metadata."""
    input_tokens = 0
    output_tokens = 0
    usage_samples = 0
    by_request_kind: dict[str, dict[str, int]] = defaultdict(
        lambda: {"sample_count": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    )
    for sample in samples:
        usage = extract_token_usage(sample)
        if usage is None:
            continue
        usage_samples += 1
        input_tokens += usage["input_tokens"]
        output_tokens += usage["output_tokens"]
        request_kind = str(sample.get("request_kind") or "unknown")
        bucket = by_request_kind[request_kind]
        bucket["sample_count"] += 1
        bucket["input_tokens"] += usage["input_tokens"]
        bucket["output_tokens"] += usage["output_tokens"]
        bucket["total_tokens"] += usage["total_tokens"]
    return {
        "status": "observed" if usage_samples else "not_run",
        "sample_count": usage_samples,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "by_request_kind": dict(sorted(by_request_kind.items())),
    }


def extract_token_usage(sample: dict[str, Any]) -> dict[str, int] | None:
    metadata = sample.get("metadata") or {}
    usage = metadata.get("token_usage") or metadata.get("usage") or {}
    if not isinstance(usage, dict):
        return None
    prompt = _number(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = _number(usage.get("completion_tokens", usage.get("output_tokens")))
    total = _number(usage.get("total_tokens"))
    if prompt is None and completion is None and total is None:
        return None
    input_tokens = int(prompt or 0)
    output_tokens = int(completion or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(total if total is not None else input_tokens + output_tokens),
    }


def distribution(values: Iterable[float]) -> dict[str, float | int]:
    """Return stable descriptive statistics for observed values."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {
            "count": 0,
            "min": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "median": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "stddev": 0.0,
        }
    return {
        "count": len(ordered),
        "min": round(ordered[0], 2),
        "max": round(ordered[-1], 2),
        "mean": round(statistics.fmean(ordered), 2),
        "median": round(statistics.median(ordered), 2),
        "p50": percentile(ordered, 0.50),
        "p95": percentile(ordered, 0.95),
        "p99": percentile(ordered, 0.99),
        "stddev": round(statistics.pstdev(ordered), 2),
    }


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, math.ceil(quantile * len(values)) - 1))
    return round(values[index], 2)


def build_request_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse event observations into trace/request-level latency samples."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        key = str(sample.get("request_id") or sample.get("trace_id") or sample["event_id"])
        grouped[key].append(sample)

    requests = []
    for request_id, events in grouped.items():
        request_events = [
            event
            for event in events
            if event["event_type"] in {"request", "request_complete", "workflow_complete"}
            or event["metadata"].get("is_request_summary") is True
        ]
        if not request_events:
            continue
        source_events = request_events
        latency = max((event["latency_ms"] for event in source_events), default=0.0)
        metadata = _merge_metadata(source_events)
        requests.append(
            {
                "request_id": request_id,
                "trace_id": events[0]["trace_id"],
                "incident_id": events[0]["incident_id"],
                "request_kind": _first_nonempty(event["request_kind"] for event in source_events)
                or "unknown",
                "evidence_level": events[0]["evidence_level"],
                "status": (
                    "failed"
                    if any(event["status"] not in {"success", "completed"} for event in events)
                    else "success"
                ),
                "latency_ms": latency,
                "event_count": len(events),
                "metadata": metadata,
            }
        )
    return sorted(requests, key=lambda item: (item["trace_id"], item["request_id"]))


def aggregate_group_latency(
    samples: list[dict[str, Any]],
    key: str,
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        value = str(sample.get(key) or "").strip()
        if value:
            grouped[value].append(sample["latency_ms"])
    return {name: distribution(values) for name, values in sorted(grouped.items())}


def aggregate_llm_reliability(samples: list[dict[str, Any]]) -> dict[str, Any]:
    llm_samples = [
        sample
        for sample in samples
        if sample["event_type"] == "llm_call"
        or sample["metadata"].get("provider")
        or sample["metadata"].get("model")
        or extract_token_usage(sample) is not None
    ]
    successes = sum(sample["status"] in {"success", "completed"} for sample in llm_samples)
    timeouts = sum(
        sample["status"] == "timeout"
        or bool(sample["metadata"].get("timeout"))
        or str(sample["metadata"].get("error_type") or "").lower() == "timeout"
        for sample in llm_samples
    )
    retried = sum(
        int(_number(sample["metadata"].get("attempt")) or 1) > 1 for sample in llm_samples
    )
    retry_recovered = sum(
        int(_number(sample["metadata"].get("attempt")) or 1) > 1
        and sample["status"] in {"success", "completed"}
        for sample in llm_samples
    )
    count = len(llm_samples)
    return {
        "status": "observed" if count else "not_run",
        "sample_count": count,
        "success_count": successes,
        "success_rate": round(successes / count, 4) if count else None,
        "timeout_count": timeouts,
        "timeout_rate": round(timeouts / count, 4) if count else None,
        "retried_count": retried,
        "retry_recovered_count": retry_recovered,
        "retry_recovery_rate": round(retry_recovered / retried, 4) if retried else None,
        "providers": sorted(
            {
                str(sample["metadata"].get("provider"))
                for sample in llm_samples
                if sample["metadata"].get("provider")
            }
        ),
        "models": sorted(
            {
                str(sample["metadata"].get("model"))
                for sample in llm_samples
                if sample["metadata"].get("model")
            }
        ),
    }


def load_price_snapshot(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {
            "status": "not_run",
            "reason": "No dated, source-attributed price snapshot was provided.",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "invalid", "reason": f"Price snapshot could not be read: {exc}"}
    required = {"effective_date", "source", "unit", "models"}
    missing = sorted(required.difference(payload))
    if missing:
        return {"status": "invalid", "reason": f"Price snapshot missing fields: {missing}"}
    if payload.get("unit") not in VALID_PRICE_UNITS:
        return {
            "status": "invalid",
            "reason": f"Unsupported price unit: {payload.get('unit')}",
        }
    if not isinstance(payload.get("models"), dict):
        return {"status": "invalid", "reason": "Price snapshot models must be an object."}
    return {"status": "loaded", **payload, "path": str(path)}


def calculate_cost(
    samples: list[dict[str, Any]],
    price_snapshot: dict[str, Any],
) -> dict[str, Any]:
    if price_snapshot.get("status") != "loaded":
        return {
            "status": "not_run",
            "reason": price_snapshot.get("reason", "Price snapshot unavailable."),
            "priced_sample_count": 0,
            "unpriced_sample_count": sum(extract_token_usage(item) is not None for item in samples),
        }

    models = price_snapshot["models"]
    currency = "USD" if price_snapshot["unit"].startswith("usd_") else "CNY"
    total = 0.0
    priced = 0
    unpriced = []
    by_request_kind: dict[str, float] = defaultdict(float)
    for sample in samples:
        usage = extract_token_usage(sample)
        if usage is None:
            continue
        model = str(sample["metadata"].get("model") or "").strip()
        price = models.get(model)
        if not isinstance(price, dict):
            unpriced.append({"event_id": sample["event_id"], "model": model or "unknown"})
            continue
        input_rate = _number(price.get("input"))
        output_rate = _number(price.get("output"))
        if input_rate is None or output_rate is None:
            unpriced.append({"event_id": sample["event_id"], "model": model or "unknown"})
            continue
        sample_cost = (
            usage["input_tokens"] * input_rate + usage["output_tokens"] * output_rate
        ) / 1_000_000
        total += sample_cost
        priced += 1
        by_request_kind[str(sample.get("request_kind") or "unknown")] += sample_cost
    return {
        "status": "calculated" if priced else "not_run",
        "currency": currency,
        "amount": round(total, 8) if priced else None,
        "priced_sample_count": priced,
        "unpriced_sample_count": len(unpriced),
        "unpriced_samples": unpriced[:20],
        "by_request_kind": {key: round(value, 8) for key, value in sorted(by_request_kind.items())},
        "price_snapshot": {
            "effective_date": price_snapshot["effective_date"],
            "source": price_snapshot["source"],
            "unit": price_snapshot["unit"],
            "path": price_snapshot["path"],
        },
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    latency = summary["latency_ms"]
    usage = summary["token_usage"]
    coverage = summary["required_real_request_coverage"]
    return "\n".join(
        [
            "# AutoOnCall Performance Snapshot",
            "",
            f"- Status: `{summary['status']}`",
            f"- Reason: {summary['reason']}",
            f"- Evidence level: `{payload['run']['evidence_level']}`",
            f"- Event/request samples: `{summary['event_sample_count']}/"
            f"{summary['request_sample_count']}`",
            f"- Real request gate: `{summary['real_request_acceptance_gate']}`",
            f"- Required RAG requests: `{coverage['rag']['observed']}/"
            f"{coverage['rag']['required']}`",
            f"- Required AIOps requests: `{coverage['aiops']['observed']}/"
            f"{coverage['aiops']['required']}`",
            *provenance_markdown_lines(payload["run"]["environment"]),
            "",
            "## End-to-end Latency",
            "",
            f"- P50/P95/P99: `{latency['p50']}/{latency['p95']}/{latency['p99']} ms`",
            f"- Mean/stddev: `{latency['mean']} / {latency['stddev']} ms`",
            "",
            "## Token Usage",
            "",
            f"- Status: `{usage['status']}`",
            f"- Usage samples: `{usage['sample_count']}`",
            f"- Input/output/total: `{usage['input_tokens']}/"
            f"{usage['output_tokens']}/{usage['total_tokens']}`",
            "",
            "## Cost Boundary",
            "",
            f"- Status: `{summary['cost']['status']}`",
            f"- Reason: {summary['cost'].get('reason', 'Dated price snapshot applied.')}",
            "",
            "## Evidence Boundary",
            "",
            f"- Observed levels: `{', '.join(payload['run']['observed_evidence_levels']) or 'none'}`",
            f"- Evidence conflicts: `{summary['evidence']['conflict_count']}`",
            "- Fixture, local live, controlled fault, and production results are never merged.",
            "",
        ]
    )


def write_outputs(payload: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    md_path.write_text(render_markdown(payload), "utf-8")


def _event_sample(event: Any, *, evidence_level: str) -> dict[str, Any] | None:
    payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event)
    latency = _number(payload.get("latency_ms"))
    if latency is None:
        return None
    metadata = dict(payload.get("metadata") or {})
    observed_level = str(metadata.get("evidence_level") or "unclassified")
    event_type = str(payload.get("event_type") or "")
    node_name = str(payload.get("node_name") or payload.get("node") or "")
    return {
        "event_id": str(payload.get("event_id") or ""),
        "trace_id": str(payload.get("trace_id") or ""),
        "request_id": str(metadata.get("request_id") or ""),
        "incident_id": str(payload.get("incident_id") or ""),
        "event_type": event_type,
        "node": node_name,
        "phase": _phase_name(metadata.get("phase") or node_name),
        "tool_name": str(payload.get("tool_name") or metadata.get("tool_name") or ""),
        "request_kind": str(metadata.get("request_kind") or _infer_request_kind(payload)),
        "latency_ms": latency,
        "status": str(payload.get("status") or "unknown"),
        "evidence_level": observed_level,
        "created_at": payload.get("created_at"),
        "metadata": metadata,
    }


def _infer_request_kind(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata") or {}
    path = str(metadata.get("path") or metadata.get("endpoint") or "").lower()
    if "chat" in path or str(payload.get("node_name") or "").lower().startswith("rag"):
        return "rag"
    if "aiops" in path:
        return "aiops"
    event_type = str(payload.get("event_type") or "").lower()
    node_name = str(payload.get("node_name") or "").lower()
    if event_type in {"request", "request_complete", "workflow_complete"} and (
        payload.get("incident_id")
        or node_name in {"workflow", "planner", "executor", "replanner", "report"}
    ):
        return "aiops"
    return "unknown"


def _phase_name(value: Any) -> str:
    phase = str(value or "").strip().lower()
    return PHASE_ALIASES.get(phase, phase)


def _merge_metadata(samples: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for sample in samples:
        for key, value in sample["metadata"].items():
            if value not in (None, "", [], {}):
                merged[key] = value
    return merged


def _metadata_numbers(samples: list[dict[str, Any]], *keys: str) -> list[float]:
    values = []
    for sample in samples:
        metadata = sample.get("metadata") or {}
        for key in keys:
            value = _number(metadata.get(key))
            if value is not None:
                values.append(value)
                break
    return values


def _first_nonempty(values: Iterable[Any]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _coverage_entry(observed: int, required: int) -> dict[str, int | str]:
    normalized_required = max(0, int(required))
    return {
        "observed": int(observed),
        "required": normalized_required,
        "status": "met" if observed >= normalized_required else "not_met",
    }


def _evaluation_status(
    *,
    samples: list[dict[str, Any]],
    evidence_conflicts: list[dict[str, Any]],
    real_request_gate: bool,
) -> str:
    if not samples:
        return "not_run"
    if evidence_conflicts:
        return "invalid_evidence"
    if real_request_gate:
        return "passed"
    return "observed_not_accepted"


def _status_reason(
    *,
    status: str,
    evidence_level: str,
    samples: list[dict[str, Any]],
    evidence_conflicts: list[dict[str, Any]],
    coverage: dict[str, dict[str, int | str]],
) -> str:
    if status == "not_run":
        return "No persisted performance events were available."
    if evidence_conflicts:
        return "Persisted events contain evidence levels that conflict with the requested run."
    if evidence_level == "offline_fixture":
        return (
            "Fixture observations are reported but cannot satisfy the real-model acceptance gate."
        )
    missing = [
        f"{kind}={item['observed']}/{item['required']}"
        for kind, item in coverage.items()
        if item["status"] != "met"
    ]
    if missing:
        return "Real request coverage is below the stage-6 acceptance minimum: " + ", ".join(
            missing
        )
    return f"Accepted {len(samples)} persisted events with strictly matching evidence."


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--evidence-level",
        choices=sorted(EVIDENCE_LEVELS | {"unclassified"}),
        default="offline_fixture",
        help="The level actually represented by the selected persisted events.",
    )
    parser.add_argument("--price-snapshot", default="")
    parser.add_argument("--required-rag-requests", type=int, default=DEFAULT_REQUIRED_RAG_REQUESTS)
    parser.add_argument(
        "--required-aiops-requests",
        type=int,
        default=DEFAULT_REQUIRED_AIOPS_REQUESTS,
    )
    parser.add_argument("--summary-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--summary-md", default=str(DEFAULT_OUTPUT_MD))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    price_snapshot_path = Path(args.price_snapshot) if args.price_snapshot else None
    payload = evaluate_performance(
        limit=args.limit,
        evidence_level=args.evidence_level,
        price_snapshot_path=price_snapshot_path,
        required_rag_requests=args.required_rag_requests,
        required_aiops_requests=args.required_aiops_requests,
    )
    write_outputs(payload, Path(args.summary_json), Path(args.summary_md))
    print(
        "Performance snapshot: "
        f"{payload['summary']['status']}; "
        f"events={payload['summary']['event_sample_count']}; "
        f"requests={payload['summary']['request_sample_count']}; "
        f"evidence={payload['run']['evidence_level']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
