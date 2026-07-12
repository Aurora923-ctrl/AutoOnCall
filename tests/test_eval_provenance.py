"""Evaluation artifact provenance and staleness tests."""

from scripts.eval.eval_environment import (
    assess_eval_artifact_staleness,
    collect_eval_environment,
    collect_worktree_state,
)


def test_eval_environment_contains_reproducibility_metadata() -> None:
    environment = collect_eval_environment(suite="test")

    assert environment["git_commit"]
    assert isinstance(environment["git_dirty"], bool)
    assert environment["git_worktree_sha256"]
    assert environment["asset_manifest"]["file_count"] == 20
    assert environment["asset_manifest_sha256"]
    assert environment["evidence_level"] == "offline_fixture"
    assert environment["dependency_manifest"]["package_count"] > 0
    assert environment["prompt_version"].startswith("prompt-")
    assert environment["prompt_manifest"]["file_count"] >= 4
    assert isinstance(environment["baseline_eligible"], bool)
    assert environment["config_summary"]["index_allowed_roots"] == "uploads,docs/knowledge-base"
    assert environment["config_sha256"]
    assert environment["evaluation_fingerprint"]
    assert environment["artifact_status"]["stale"] is False


def test_current_eval_environment_is_not_stale() -> None:
    environment = collect_eval_environment(suite="test")

    status = assess_eval_artifact_staleness({"environment": environment})

    assert status["stale"] is False
    assert status["reasons"] == []


def test_changed_asset_manifest_marks_artifact_stale() -> None:
    environment = collect_eval_environment(suite="test")
    environment["asset_manifest_sha256"] = "stale-assets"

    status = assess_eval_artifact_staleness({"environment": environment})

    assert status["stale"] is True
    assert "knowledge_assets_changed" in status["reasons"]


def test_changed_worktree_marks_artifact_stale() -> None:
    environment = collect_eval_environment(suite="test")
    environment["git_worktree_sha256"] = "stale-worktree"

    status = assess_eval_artifact_staleness({"environment": environment})

    assert status["stale"] is True
    assert "git_worktree_changed" in status["reasons"]


def test_changed_prompt_manifest_marks_artifact_stale() -> None:
    environment = collect_eval_environment(suite="test")
    environment["prompt_manifest_sha256"] = "stale-prompts"

    status = assess_eval_artifact_staleness({"environment": environment})

    assert status["stale"] is True
    assert "prompts_changed" in status["reasons"]


def test_worktree_hash_changes_when_untracked_content_changes(tmp_path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    path = tmp_path / "draft.txt"
    path.write_text("first", encoding="utf-8")
    first = collect_worktree_state(tmp_path)
    path.write_text("second", encoding="utf-8")
    second = collect_worktree_state(tmp_path)

    assert first["worktree_sha256"] != second["worktree_sha256"]
