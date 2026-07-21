"""Tests for timestamped benchmark baseline manifests."""

import json

from scripts.eval.run_benchmark_baseline import (
    MODULES,
    artifact_status,
    build_official_block_reasons,
    reserve_run_directory,
    write_failed_pointer,
    write_interview_scorecard,
)


def test_reserve_run_directory_never_overwrites_history(tmp_path) -> None:
    first = reserve_run_directory(tmp_path, "run")
    second = reserve_run_directory(tmp_path, "run")

    assert first.name == "run"
    assert second.name == "run-01"


def test_dirty_worktree_blocks_official_baseline() -> None:
    reasons = build_official_block_reasons(
        environment={"git_dirty": True, "git_commit": "abc"},
        modules=[
            {
                "id": "rag",
                "status": "passed",
                "artifact_status": {"stale": False},
            }
        ],
    )

    assert reasons == ["dirty_worktree"]


def test_stale_module_blocks_official_baseline_reason() -> None:
    reasons = build_official_block_reasons(
        environment={"git_dirty": False, "git_commit": "abc"},
        modules=[
            {
                "id": "rag",
                "status": "passed",
                "artifact_status": {"stale": True},
            }
        ],
    )

    assert reasons == ["rag:stale"]


def test_observed_performance_smoke_is_incomplete_baseline_evidence() -> None:
    assert artifact_status({"summary": {"status": "observed_not_accepted"}}) == "incomplete"


def test_local_knowledge_checks_without_milvus_are_incomplete_baseline_evidence() -> None:
    assert artifact_status({"summary": {"status": "passed_without_milvus"}}) == "incomplete"


def test_benchmark_uses_the_stable_rag_delivery_contract() -> None:
    rag = next(module for module in MODULES if module["id"] == "rag")

    assert "eval/rag_cases.yaml" in rag["extra_args"]
    assert "eval/rag_relevance_cases.yaml" not in rag["extra_args"]


def test_ragas_is_optional_benchmark_evidence() -> None:
    ragas = next(module for module in MODULES if module["id"] == "ragas")

    assert ragas["required"] is False


def test_benchmark_writes_scorecard_into_same_run_directory(tmp_path) -> None:
    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    manifest_path = run_dir / "baseline_manifest.json"
    payload = {
        "run": {
            "run_id": "run-001",
            "environment": {"git_commit": "abc123"},
        },
        "summary": {
            "status": "passed",
            "baseline_status": "candidate_dirty_worktree",
            "official_baseline": False,
            "module_count": 0,
            "required_module_count": 0,
            "required_passed_module_count": 0,
            "optional_module_count": 0,
            "failed_module_count": 0,
            "metrics": {},
            "official_block_reasons": ["dirty_worktree"],
        },
        "modules": [],
    }

    write_interview_scorecard(payload, manifest_json=manifest_path, run_dir=run_dir)

    scorecard = json.loads((run_dir / "interview_scorecard.json").read_text("utf-8"))
    assert scorecard["run"]["run_id"] == "run-001"
    assert (run_dir / "interview_scorecard.md").exists()


def test_failed_benchmark_does_not_replace_latest_pass_pointer(tmp_path) -> None:
    run_dir = tmp_path / "failed-run"
    run_dir.mkdir()
    manifest_json = run_dir / "baseline_manifest.json"
    manifest_md = run_dir / "baseline_manifest.md"
    manifest_json.write_text("{}", encoding="utf-8")
    manifest_md.write_text("# failed\n", encoding="utf-8")
    payload = {
        "run": {"run_id": "failed-run", "ended_at": "2026-07-21T00:00:00+00:00"},
        "summary": {"status": "failed", "baseline_status": "candidate_incomplete"},
    }

    write_failed_pointer(tmp_path, payload, manifest_json, manifest_md)

    assert not (tmp_path / "latest.json").exists()
    assert json.loads((tmp_path / "latest_failed.json").read_text("utf-8"))["run_id"] == "failed-run"
