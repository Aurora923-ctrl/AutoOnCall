"""Tests for the bounded real-model acceptance runner."""

from scripts.performance.run_real_model_acceptance import _request_summary


def test_request_summary_reports_stage6_acceptance_and_latency() -> None:
    payload = _request_summary(
        [
            {"passed": True, "latency_ms": 100.0},
            {"passed": True, "latency_ms": 200.0},
            {"passed": False, "latency_ms": 50.0},
        ],
        required=2,
    )

    assert payload["acceptance_status"] == "met"
    assert payload["observed"] == 3
    assert payload["passed"] == 2
    assert payload["failed"] == 1
    assert payload["latency_ms"]["p50"] == 100.0
    assert payload["latency_ms"]["p95"] == 200.0
