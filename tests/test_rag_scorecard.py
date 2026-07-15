"""Tests for RAG scorecard provenance and layer-status gates."""

from scripts.eval.build_rag_scorecard import (
    artifact_validation,
    failed_case_ids,
    layer_status,
    provenance_signature,
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

    assert provenance_signature({"provenance": left}) != provenance_signature(
        {"provenance": right}
    )


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
