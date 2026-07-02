"""Tests for the read-only evaluation summary API."""

import importlib
import json

import pytest


@pytest.mark.asyncio
async def test_eval_summary_api_returns_resume_metrics(monkeypatch, tmp_path) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    summary_path = tmp_path / "eval_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "run": {
                    "evaluation_scope": "offline deterministic regression",
                    "started_at": "2026-06-26T00:00:00+00:00",
                },
                "summary": {
                    "overall_case_count": 38,
                    "overall_passed_count": 38,
                    "overall_pass_rate": 1.0,
                    "resume_metrics": {
                        "aiops_case_count": 16,
                        "aiops_pass_rate": 1.0,
                        "rag_case_count": 22,
                        "root_cause_hit_rate": 1.0,
                        "tool_hit_rate": 1.0,
                        "approval_recall": 1.0,
                        "forbidden_action_block_rate": 1.0,
                        "rag_recall_at_k": 1.0,
                        "rag_citation_coverage_rate": 1.0,
                        "rag_no_answer_rejection_rate": 1.0,
                        "p95_latency_ms": 20.04,
                    },
                    "categories": {
                        "rag": {
                            "mrr": 0.96,
                            "recall_at_k": 1.0,
                            "citation_coverage_rate": 1.0,
                        },
                        "risk": {"forbidden_action_block_rate": 1.0},
                    },
                    "failed_cases": [],
                },
                "rag": {
                    "summary": {
                        "case_count": 22,
                        "pass_rate": 1.0,
                        "mrr": 0.96,
                        "citation_coverage_rate": 1.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(evaluations_api, "EVAL_SUMMARY_PATH", summary_path)

    payload = await evaluations_api.get_eval_summary()

    assert payload["available"] is True
    assert payload["summary"]["overall_case_count"] == 38
    assert payload["resume_metrics"]["aiops_case_count"] == 16
    assert payload["rag"]["mrr"] == 0.96
    assert payload["run"]["evaluation_scope"] == "offline deterministic regression"
    metric_by_key = {metric["key"]: metric for metric in payload["dashboard"]["metrics"]}
    assert payload["dashboard"]["generated_at"] == "2026-06-26T00:00:00+00:00"
    assert metric_by_key["total_cases"]["label"] == "总用例数"
    assert metric_by_key["aiops_pass_rate"]["value"] == 1.0
    assert metric_by_key["rag_pass_rate"]["value"] == 1.0
    assert metric_by_key["forbidden_action_block_rate"]["value"] == 1.0
    assert metric_by_key["rag_citation_pass_rate"]["value"] == 1.0
    assert metric_by_key["p95_latency_ms"]["value_type"] == "duration_ms"


@pytest.mark.asyncio
async def test_eval_summary_api_returns_unavailable_when_missing(monkeypatch, tmp_path) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    monkeypatch.setattr(evaluations_api, "EVAL_SUMMARY_PATH", tmp_path / "missing.json")

    payload = await evaluations_api.get_eval_summary()

    assert payload["available"] is False
    assert payload["summary"] is None
    assert payload["resume_metrics"] == {}
    assert payload["dashboard"]["metrics"] == []
    assert "not been generated" in payload["message"]


@pytest.mark.asyncio
async def test_eval_summary_api_returns_unavailable_for_invalid_json(
    monkeypatch,
    tmp_path,
) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    summary_path = tmp_path / "eval_summary.json"
    summary_path.write_text("{invalid", encoding="utf-8")
    monkeypatch.setattr(evaluations_api, "EVAL_SUMMARY_PATH", summary_path)

    payload = await evaluations_api.get_eval_summary()

    assert payload["available"] is False
    assert payload["failed_cases"] == []
    assert "unreadable" in payload["message"]


@pytest.mark.asyncio
async def test_adapter_verification_api_returns_latest_payload(monkeypatch, tmp_path) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    adapter_path = tmp_path / "full_stack_adapter_verification.json"
    adapter_path.write_text(
        json.dumps(
            {
                "status": "passed",
                "summary": "all adapters verified",
                "checks": [{"tool_name": "query_metrics", "status": "passed"}],
                "data_sources": ["prometheus"],
                "failed_tools": [],
                "duration_ms": 12.5,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(evaluations_api, "ADAPTER_VERIFICATION_PATH", adapter_path)

    payload = await evaluations_api.get_adapter_verification()

    assert payload["available"] is True
    assert payload["status"] == "passed"
    assert payload["checks"][0]["tool_name"] == "query_metrics"
    assert "path" not in payload


@pytest.mark.asyncio
async def test_adapter_verification_api_returns_unavailable_when_missing(
    monkeypatch,
    tmp_path,
) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    monkeypatch.setattr(
        evaluations_api,
        "ADAPTER_VERIFICATION_PATH",
        tmp_path / "missing-adapter-verification.json",
    )

    payload = await evaluations_api.get_adapter_verification()

    assert payload["available"] is False
    assert payload["status"] == "missing"
    assert payload["checks"] == []
    assert "not been generated" in payload["message"]
