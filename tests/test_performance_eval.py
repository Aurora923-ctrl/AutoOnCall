"""Tests for performance distribution, evidence boundaries, and token accounting."""

import json

from app.models.trace import TraceEvent
from app.services.aiops_store import create_aiops_store
from scripts.eval.eval_performance import (
    aggregate_token_usage,
    calculate_cost,
    distribution,
    evaluate_performance,
    load_price_snapshot,
)


def test_distribution_reports_percentiles_and_stddev() -> None:
    result = distribution([10, 20, 30, 40, 50])

    assert result["count"] == 5
    assert result["p50"] == 30
    assert result["p95"] == 50
    assert result["stddev"] > 0


def test_token_usage_supports_provider_field_aliases() -> None:
    result = aggregate_token_usage(
        [
            {
                "request_kind": "rag",
                "metadata": {"usage": {"input_tokens": 10, "output_tokens": 5}},
            },
            {
                "request_kind": "aiops",
                "metadata": {
                    "token_usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 8,
                    }
                },
            },
        ]
    )

    assert result["status"] == "observed"
    assert result["input_tokens"] == 30
    assert result["output_tokens"] == 13
    assert result["total_tokens"] == 43
    assert result["by_request_kind"]["rag"]["total_tokens"] == 15


def test_evaluator_marks_empty_real_run_not_run(tmp_path) -> None:
    store = create_aiops_store(tmp_path / "performance.db")

    result = evaluate_performance(
        store=store,
        evidence_level="local_live",
        required_rag_requests=1,
        required_aiops_requests=1,
    )

    assert result["summary"]["status"] == "not_run"
    assert result["summary"]["real_request_acceptance_gate"] == "not_run"
    assert result["summary"]["token_usage"]["status"] == "not_run"


def test_fixture_data_cannot_satisfy_real_request_gate(tmp_path) -> None:
    store = create_aiops_store(tmp_path / "performance.db")
    store.save_trace_event(
        TraceEvent(
            trace_id="trace-fixture",
            incident_id="INC-FIXTURE",
            event_type="request_complete",
            node_name="workflow",
            latency_ms=25,
            metadata={
                "request_kind": "aiops",
                "evidence_level": "offline_fixture",
                "token_usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )
    )

    result = evaluate_performance(
        store=store,
        evidence_level="offline_fixture",
        required_rag_requests=0,
        required_aiops_requests=1,
    )

    assert result["summary"]["status"] == "observed_not_accepted"
    assert result["summary"]["real_request_acceptance_gate"] == "not_run"
    assert result["run"]["observed_evidence_levels"] == ["offline_fixture"]


def test_evidence_level_conflict_is_rejected(tmp_path) -> None:
    store = create_aiops_store(tmp_path / "performance.db")
    store.save_trace_event(
        TraceEvent(
            trace_id="trace-conflict",
            incident_id="INC-CONFLICT",
            event_type="request_complete",
            node_name="workflow",
            latency_ms=25,
            metadata={
                "request_kind": "aiops",
                "evidence_level": "offline_fixture",
            },
        )
    )

    result = evaluate_performance(
        store=store,
        evidence_level="local_live",
        required_rag_requests=0,
        required_aiops_requests=1,
    )

    assert result["summary"]["status"] == "invalid_evidence"
    assert result["summary"]["evidence"]["conflict_count"] == 1


def test_internal_trace_events_do_not_count_as_completed_requests(tmp_path) -> None:
    store = create_aiops_store(tmp_path / "performance.db")
    store.save_trace_event(
        TraceEvent(
            trace_id="trace-internal",
            incident_id="INC-INTERNAL",
            event_type="approval_request",
            node_name="approval_service",
            latency_ms=5,
            metadata={"evidence_level": "offline_fixture"},
        )
    )

    result = evaluate_performance(store=store, evidence_level="offline_fixture")

    assert result["summary"]["request_counts"] == {}
    assert result["requests"] == []
    assert result["summary"]["latency_ms"]["count"] == 0


def test_dated_price_snapshot_calculates_only_known_models(tmp_path) -> None:
    snapshot_path = tmp_path / "prices.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "effective_date": "2026-07-11",
                "source": "provider billing page captured by operator",
                "unit": "usd_per_million_tokens",
                "models": {"model-a": {"input": 1.0, "output": 2.0}},
            }
        ),
        encoding="utf-8",
    )
    snapshot = load_price_snapshot(snapshot_path)

    result = calculate_cost(
        [
            {
                "event_id": "evt-1",
                "request_kind": "rag",
                "metadata": {
                    "model": "model-a",
                    "usage": {"input_tokens": 1_000_000, "output_tokens": 500_000},
                },
            },
            {
                "event_id": "evt-2",
                "request_kind": "aiops",
                "metadata": {
                    "model": "unknown-model",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ],
        snapshot,
    )

    assert result["status"] == "calculated"
    assert result["amount"] == 2.0
    assert result["priced_sample_count"] == 1
    assert result["unpriced_sample_count"] == 1
