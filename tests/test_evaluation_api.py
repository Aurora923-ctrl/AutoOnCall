"""Tests for the read-only evaluation summary API."""

import importlib
import json

import pytest

from app.services.aiops_read_models.replay_evaluation import replay_evaluation_provenance


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
    backlog_path = tmp_path / "eval_backlog_drafts.json"
    backlog_path.write_text(
        json.dumps(
            {
                "summary": {
                    "total": 1,
                    "by_target": {"aiops": 1},
                    "by_category": {"tool_failure": 1},
                    "by_priority": {"P0": 1},
                    "by_review_status": {"new": 1},
                    "by_eval_file": {"eval/cases.yaml": 1},
                },
                "items": [
                    {
                        "suggested_eval_case_id": "draft_aiops_timeout",
                        "target": "aiops",
                        "category": "tool_failure",
                        "priority": "P0",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(evaluations_api, "EVAL_BACKLOG_PATH", backlog_path)

    payload = await evaluations_api.get_eval_summary()

    assert payload["available"] is False
    assert payload["stale"] is True
    assert "missing_evaluation_fingerprint" in payload["artifact_status"]["reasons"]
    assert payload["path"] == "eval_summary.json"
    assert payload["artifact"] == "eval_summary.json"
    assert str(tmp_path) not in payload["path"]
    assert payload["summary"]["overall_case_count"] == 38
    assert payload["resume_metrics"]["aiops_case_count"] == 16
    assert payload["rag"]["mrr"] == 0.96
    assert payload["run"]["evaluation_scope"] == "offline deterministic regression"
    metric_by_key = {metric["key"]: metric for metric in payload["dashboard"]["metrics"]}
    assert payload["eval_backlog"]["available"] is True
    assert payload["eval_backlog"]["summary"]["total"] == 1
    assert payload["eval_backlog"]["items"][0]["suggested_eval_case_id"] == "draft_aiops_timeout"
    assert payload["dashboard"]["generated_at"] == "2026-06-26T00:00:00+00:00"
    assert metric_by_key["total_cases"]["label"] == "总用例数"
    assert metric_by_key["aiops_pass_rate"]["value"] == 1.0
    assert metric_by_key["rag_pass_rate"]["value"] == 1.0
    assert metric_by_key["forbidden_action_block_rate"]["value"] == 1.0
    assert metric_by_key["rag_retrieval_citation_metadata_rate"]["value"] == 1.0
    assert metric_by_key["p95_latency_ms"]["value_type"] == "duration_ms"


@pytest.mark.asyncio
async def test_eval_summary_api_returns_unavailable_when_missing(monkeypatch, tmp_path) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    monkeypatch.setattr(evaluations_api, "EVAL_SUMMARY_PATH", tmp_path / "missing.json")

    payload = await evaluations_api.get_eval_summary()

    assert payload["available"] is False
    assert payload["path"] == "missing.json"
    assert payload["artifact"] == "missing.json"
    assert str(tmp_path) not in payload["path"]
    assert payload["summary"] is None
    assert payload["resume_metrics"] == {}
    assert payload["dashboard"]["metrics"] == []
    assert payload["eval_backlog"]["available"] is False
    assert "not been generated" in payload["message"]


@pytest.mark.asyncio
async def test_eval_summary_api_reads_config_path_at_request_time(monkeypatch, tmp_path) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text(
        json.dumps({"summary": {"overall_case_count": 1, "overall_passed_count": 1}}),
        encoding="utf-8",
    )
    second_path.write_text(
        json.dumps({"summary": {"overall_case_count": 2, "overall_passed_count": 2}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(evaluations_api, "EVAL_SUMMARY_PATH", None)
    monkeypatch.setattr(evaluations_api, "EVAL_BACKLOG_PATH", None)

    monkeypatch.setattr(evaluations_api.config, "eval_summary_path", str(first_path))
    first_payload = await evaluations_api.get_eval_summary()
    monkeypatch.setattr(evaluations_api.config, "eval_summary_path", str(second_path))
    second_payload = await evaluations_api.get_eval_summary()

    assert first_payload["summary"]["overall_case_count"] == 1
    assert second_payload["summary"]["overall_case_count"] == 2


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
    assert payload["path"] == "eval_summary.json"
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
async def test_ragas_summary_api_returns_quality_dashboard(monkeypatch, tmp_path) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    ragas_path = tmp_path / "ragas_eval_summary.json"
    ragas_path.write_text(
        json.dumps(
            {
                "run": {
                    "ended_at": "2026-07-08T10:00:00+00:00",
                    "evaluation_scope": "optional RAGAS quality regression",
                    "cases_path": "eval/rag_cases.yaml",
                    "docs_dir": "docs/knowledge-base",
                    "answer_source": "reference-fixture",
                    "metric_profile": "id-smoke",
                    "judge_model": "qwen-max",
                    "embedding_model": "text-embedding-v4",
                    "artifacts": {
                        "summary_json": "logs/ragas_eval_summary.json",
                        "summary_md": "logs/ragas_eval_summary.md",
                    },
                },
                "thresholds": {
                    "id_context_recall": 0.75,
                    "oncall_actionability": 0.8,
                },
                "quality_contract": {
                    "status": "passed",
                    "hard_gates": [
                        {
                            "key": "core_case_pass_rate",
                            "status": "passed",
                            "value": 1.0,
                            "threshold": 1.0,
                        }
                    ],
                    "watch_metrics": [],
                },
                "summary": {
                    "status": "passed",
                    "case_count": 3,
                    "passed_count": 3,
                    "pass_rate": 1.0,
                    "core_case_pass_rate": 1.0,
                    "id_context_precision_avg": 0.83,
                    "id_context_recall_avg": 1.0,
                    "oncall_actionability_avg": 1.0,
                    "refusal_boundary_rate": 1.0,
                    "faithfulness_avg": 0.0,
                    "response_relevancy_avg": 0.0,
                    "failed_cases": [],
                },
                "case_scores": [
                    {
                        "id": "payment_mysql_smoke",
                        "passed": True,
                        "metrics": {"id_based_context_recall": 1.0},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(evaluations_api, "RAGAS_SUMMARY_PATH", ragas_path)

    payload = await evaluations_api.get_ragas_summary()

    assert payload["available"] is False
    assert payload["stale"] is True
    assert "missing_evaluation_fingerprint" in payload["artifact_status"]["reasons"]
    assert payload["path"] == "ragas_eval_summary.json"
    assert str(tmp_path) not in payload["path"]
    assert payload["summary"]["status"] == "passed"
    assert payload["dashboard"]["profile"] == "id-smoke"
    assert "--metrics-profile id-smoke" in payload["dashboard"]["command"]
    assert payload["dashboard"]["artifacts"]["summary_md"] == "logs/ragas_eval_summary.md"
    assert payload["dashboard"]["judge_model"] == "not_required_for_id_smoke"
    assert payload["quality_contract"]["status"] == "passed"
    metric_by_key = {metric["key"]: metric for metric in payload["dashboard"]["metrics"]}
    assert metric_by_key["ragas_pass_rate"]["value"] == 1.0
    assert metric_by_key["ragas_actionability"]["value"] == 1.0
    assert payload["case_scores"][0]["id"] == "payment_mysql_smoke"

    alias_payload = await evaluations_api.get_ragas_summary_alias()
    assert alias_payload["path"] == payload["path"]


@pytest.mark.asyncio
async def test_eval_backlog_api_recomputes_summary_and_validates_items(
    monkeypatch, tmp_path
) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    backlog_path = tmp_path / "backlog.json"
    backlog_path.write_text(
        json.dumps(
            {
                "summary": {"total": 999, "by_target": {"rag": 999}},
                "items": [
                    {
                        "backlog_id": "ebl-1",
                        "target": "rag",
                        "category": "missing_citation",
                        "priority": "P1",
                        "review_status": "new",
                    },
                    {"backlog_id": "invalid", "target": "not-a-target"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(evaluations_api, "EVAL_BACKLOG_PATH", backlog_path)

    payload = await evaluations_api.get_eval_backlog()

    assert payload["summary"]["total"] == 1
    assert payload["summary"]["by_target"] == {"rag": 1}
    assert len(payload["items"]) == 1
    assert len(payload["invalid_items"]) == 1


@pytest.mark.asyncio
async def test_ragas_summary_api_returns_unavailable_when_missing(
    monkeypatch,
    tmp_path,
) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    monkeypatch.setattr(evaluations_api, "RAGAS_SUMMARY_PATH", tmp_path / "missing-ragas.json")

    payload = await evaluations_api.get_ragas_summary()

    assert payload["available"] is False
    assert payload["path"] == "missing-ragas.json"
    assert payload["dashboard"]["metrics"] == []
    assert "not been generated" in payload["message"]


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
    assert payload["path"] == "missing-adapter-verification.json"
    assert payload["artifact"] == "missing-adapter-verification.json"
    assert str(tmp_path) not in payload["path"]
    assert payload["status"] == "missing"
    assert payload["checks"] == []
    assert "not been generated" in payload["message"]


@pytest.mark.asyncio
async def test_eval_backlog_api_returns_reviewable_drafts(monkeypatch, tmp_path) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    backlog_path = tmp_path / "eval_backlog_drafts.json"
    backlog_path.write_text(
        json.dumps(
            {
                "summary": {"total": 1, "by_review_status": {"new": 1}},
                "items": [
                    {
                        "feedback_id": "fbk-001",
                        "target": "rag",
                        "category": "retrieval_failure",
                        "suggested_eval_file": "eval/rag_cases.yaml",
                        "suggested_eval_case_id": "draft_rag_redis",
                        "suggested_eval_dimension": "rag_recall_at_k",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(evaluations_api, "EVAL_BACKLOG_PATH", backlog_path)

    payload = await evaluations_api.get_eval_backlog()

    assert payload["available"] is True
    assert payload["summary"]["total"] == 1
    assert payload["items"][0]["target"] == "rag"


@pytest.mark.asyncio
async def test_eval_backlog_api_returns_unavailable_when_missing(monkeypatch, tmp_path) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    monkeypatch.setattr(evaluations_api, "EVAL_BACKLOG_PATH", tmp_path / "missing.json")

    payload = await evaluations_api.get_eval_backlog()

    assert payload["available"] is False
    assert payload["summary"]["total"] == 0
    assert payload["items"] == []


@pytest.mark.asyncio
async def test_scorecard_api_reads_only_latest_run(monkeypatch, tmp_path) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    benchmark_root = tmp_path / "logs" / "benchmarks"
    run_dir = benchmark_root / "run-001"
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "baseline_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    (run_dir / "interview_scorecard.json").write_text(
        json.dumps(
            {
                "run": {"run_id": "run-001", "single_run_enforced": True},
                "summary": {"status": "passed", "production_status": "not_enough_data"},
                "modules": [
                    {
                        "key": "rag_retrieval",
                        "run_id": "run-001",
                        "evidence_level": "offline_fixture",
                        "sample_count": 80,
                        "failed_case_count": 1,
                        "artifact_path": "logs/benchmarks/run-001/rag.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    latest = benchmark_root / "latest.json"
    latest.write_text(
        json.dumps(
            {
                "run_id": "run-001",
                "manifest_json": str(manifest_path),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(evaluations_api, "BENCHMARK_LATEST_PATH", latest)
    monkeypatch.setattr(
        evaluations_api,
        "build_interview_scorecard_payload",
        lambda raw_payload, scorecard_path: {
            "available": True,
            "single_run_valid": True,
            "run": raw_payload["run"],
            "summary": raw_payload["summary"],
            "modules": raw_payload["modules"],
        },
    )

    payload = await evaluations_api.get_interview_scorecard()

    assert payload["available"] is True
    assert payload["single_run_valid"] is True
    assert payload["run"]["run_id"] == "run-001"
    assert payload["summary"]["production_status"] == "not_enough_data"
    assert payload["modules"][0]["sample_count"] == 80


@pytest.mark.asyncio
async def test_scorecard_api_rejects_scorecard_from_another_run(monkeypatch, tmp_path) -> None:
    evaluations_api = importlib.import_module("app.api.evaluations")
    run_dir = tmp_path / "logs" / "benchmarks" / "run-001"
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "baseline_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    (run_dir / "interview_scorecard.json").write_text(
        json.dumps({"run": {"run_id": "run-old"}, "summary": {}, "modules": []}),
        encoding="utf-8",
    )
    latest = run_dir.parent / "latest.json"
    latest.write_text(
        json.dumps({"run_id": "run-001", "manifest_json": str(manifest_path)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(evaluations_api, "BENCHMARK_LATEST_PATH", latest)

    payload = await evaluations_api.get_interview_scorecard()

    assert payload["available"] is False
    assert "does not belong" in payload["message"]


def test_replay_provenance_exposes_model_dataset_and_run_identity() -> None:
    provenance = replay_evaluation_provenance(
        {
            "artifact": "eval_summary.json",
            "run": {
                "run_id": "run-001",
                "judge_model": "qwen-max",
                "embedding_model": "text-embedding-v4",
                "dataset": {"path": "eval/cases.yaml", "sha256": "dataset-sha"},
                "environment": {
                    "suite": "aiops",
                    "git_commit": "commit-sha",
                    "evaluation_fingerprint": "eval-sha",
                },
            },
            "artifact_status": {"stale": False, "reasons": []},
        }
    )

    assert provenance["run_id"] == "run-001"
    assert provenance["suite"] == "aiops"
    assert provenance["model"] == "qwen-max"
    assert provenance["embedding_model"] == "text-embedding-v4"
    assert provenance["dataset"]["sha256"] == "dataset-sha"


def test_replay_provenance_prefers_actual_execution_identity() -> None:
    provenance = replay_evaluation_provenance(
        {
            "run": {
                "run_id": "run-fallback",
                "judge_model": "configured-model",
                "embedding_model": "configured-embedding",
                "environment": {
                    "suite": "ragas",
                    "execution_identity": {
                        "actual_model": "fallback-provider/model-v2",
                        "actual_embedding_model": "fallback-provider/embed-v2",
                    },
                },
            }
        }
    )

    assert provenance["model"] == "fallback-provider/model-v2"
    assert provenance["embedding_model"] == "fallback-provider/embed-v2"
