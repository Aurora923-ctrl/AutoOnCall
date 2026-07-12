"""Tests for timestamped benchmark baseline manifests."""

import json

from scripts.eval.run_benchmark_baseline import (
    build_official_block_reasons,
    reserve_run_directory,
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
