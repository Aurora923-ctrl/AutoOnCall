"""Tests for RAG scorecard provenance and layer-status gates."""

from scripts.eval.build_rag_scorecard import (
    artifact_validation,
    dataset_identity,
    failed_case_ids,
    format_optional_percent,
    format_optional_score,
    layer_status,
    provenance_signature,
    validate_dataset_binding,
)
from scripts.eval.eval_ragas_cases import (
    expected_sources,
    load_cases,
    render_markdown_summary,
)


def test_scorecard_rejects_stale_artifact() -> None:
    current = {
        "git_commit": "new",
        "git_dirty": False,
        "git_worktree_sha256": "worktree-new",
        "asset_manifest_sha256": "assets",
        "config_sha256": "config",
        "dependency_manifest_sha256": "deps",
        "prompt_manifest_sha256": "prompts",
        "evaluation_fingerprint": "fingerprint-new",
    }
    payload = {
        "run": {
            "environment": {
                **current,
                "git_commit": "old",
                "evaluation_fingerprint": "fingerprint-old",
            }
        },
        "summary": {"status": "passed"},
    }

    validation = artifact_validation("offline", payload, current_environment=current)

    assert validation["stale"] is True
    assert layer_status(validation) == "stale"


def test_scorecard_does_not_turn_failed_summary_into_pass() -> None:
    validation = {
        "name": "runtime",
        "summary_status": "failed",
        "stale": False,
        "stale_reasons": [],
        "provenance": {},
    }

    assert layer_status(validation) == "failed"


def test_scorecard_accepts_retrieval_only_status_only_when_layer_allows_it() -> None:
    validation = {
        "name": "runtime-retrieval",
        "summary_status": "retrieval_only_passed",
        "stale": False,
        "stale_reasons": [],
        "provenance": {},
    }

    assert layer_status(validation) == "failed"
    assert (
        layer_status(
            validation,
            accepted_statuses={"passed", "retrieval_only_passed"},
        )
        == "passed"
    )


def test_scorecard_detects_mixed_provenance_fields() -> None:
    left = {
        "git_commit": "commit-a",
        "git_worktree_sha256": "worktree",
        "asset_manifest_sha256": "assets",
        "config_sha256": "config",
        "dependency_manifest_sha256": "deps",
        "prompt_manifest_sha256": "prompts",
    }
    right = {**left, "prompt_manifest_sha256": "different-prompt"}

    assert provenance_signature({"provenance": left}) != provenance_signature({"provenance": right})


def test_scorecard_failed_cases_are_derived_from_artifact() -> None:
    assert failed_case_ids(
        {
            "summary": {
                "failed_cases": [
                    "plain-id",
                    {"id": "structured-id"},
                    {"failure_reasons": {"metric": "failed"}},
                ]
            }
        }
    ) == ["plain-id", "structured-id"]


def test_scorecard_dataset_identity_uses_dataset_provenance() -> None:
    assert dataset_identity(
        {
            "run": {
                "dataset": {"path": "eval/cases.yaml", "sha256": "abc"},
                "case_ids": ["b", "a"],
            }
        }
    ) == {
        "cases_path": "eval/cases.yaml",
        "sha256": "abc",
        "case_ids": ["a", "b"],
    }


def test_scorecard_rejects_missing_or_mismatched_case_sets() -> None:
    payloads = {
        name: {"run": {"case_set_sha256": "shared"}}
        for name in (
            "offline_retrieval",
            "runtime_retrieval",
            "fixed_context_generation",
            "runtime_id_smoke",
            "runtime_demo",
            "demo_chain",
            "api_contract",
        )
    }
    payloads["runtime_id_smoke"]["run"]["case_set_sha256"] = "different"

    binding = validate_dataset_binding(payloads)

    assert binding["valid"] is False
    assert binding["mismatches"] == ["fixed_context_generation_vs_runtime_id_smoke"]


def test_scorecard_rejects_same_file_hash_with_different_selected_cases() -> None:
    payloads = {
        name: {
            "run": {
                "case_set_sha256": "shared",
                "selected_case_ids": ["case-a", "case-b"],
            }
        }
        for name in (
            "offline_retrieval",
            "runtime_retrieval",
            "fixed_context_generation",
            "runtime_id_smoke",
            "runtime_demo",
            "demo_chain",
            "api_contract",
        )
    }
    payloads["runtime_id_smoke"]["run"]["selected_case_ids"] = ["case-a"]

    binding = validate_dataset_binding(payloads)

    assert binding["valid"] is False
    assert binding["mismatches"] == ["fixed_context_generation_vs_runtime_id_smoke_case_ids"]


def test_scorecard_markdown_formats_missing_metrics_without_crashing() -> None:
    assert format_optional_score(None) == "not_run"
    assert format_optional_percent(None) == "not_run"


def test_stage3_positive_cases_freeze_entities_and_source_roles() -> None:
    positive = [
        case
        for case in load_cases("eval/ragas_stage3_core_cases.yaml")
        if not case.get("should_reject")
    ]

    assert len(positive) == 10
    assert all(case.get("required_answer_entities") for case in positive)
    assert all(case.get("required_source_roles") for case in positive)

    mysql = next(case for case in positive if "pool_waiting" in str(case.get("query")))
    redis = next(
        case
        for case in positive
        if set(expected_sources(case))
        == {"official_redis_clients.md", "redis_postmortem.pdf"}
    )
    assert set(mysql["required_answer_entities"]) == {
        "pool_waiting",
        "active_connections",
        "慢查询",
        "EXPLAIN",
    }
    assert mysql["required_source_roles"] == ["runbook"]
    assert set(redis["required_answer_entities"]) == {
        "connected_clients",
        "maxclients",
        "effective_capacity",
        "blocked_clients",
    }
    assert set(redis["required_source_roles"]) == {"official", "postmortem"}


def test_ragas_report_labels_citation_membership_honestly() -> None:
    report = render_markdown_summary(
        {
            "run": {"metric_profile": "id-smoke", "environment": {}},
            "summary": {
                "status": "passed",
                "passed_count": 1,
                "case_count": 1,
                "core_case_pass_rate": 1.0,
                "faithfulness_avg": None,
                "response_relevancy_avg": None,
                "id_context_precision_avg": 1.0,
                "id_context_recall_avg": 1.0,
                "oncall_actionability_avg": 1.0,
                "citation_existence_rate": 1.0,
                "citation_membership_rate": 0.5,
                "citation_support_rate": 0.5,
                "citation_correctness_rate": 1.0,
                "factual_error_rate": 0.0,
                "severe_hallucination_rate": 0.0,
                "refusal_boundary_rate": 1.0,
                "failed_cases": [],
            },
            "quality_contract": {},
            "case_scores": [],
        }
    )

    assert "Citation membership rate: `50%`" in report
    assert "Citation support rate" not in report
