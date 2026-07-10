"""Shared environment metadata for reproducible offline evaluation reports."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.config import config

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_BASE_DIR = REPO_ROOT / "docs" / "knowledge-base"
SUPPORTED_ASSET_SUFFIXES = {
    ".md",
    ".markdown",
    ".pdf",
    ".html",
    ".htm",
    ".csv",
    ".xlsx",
}


def collect_eval_environment(*, suite: str) -> dict[str, Any]:
    """Return non-secret runtime metadata attached to every eval artifact."""
    asset_manifest = collect_asset_manifest()
    config_summary = collect_config_summary()
    git_commit = _git_output("rev-parse", "HEAD")
    git_status = _git_output("status", "--porcelain", "--untracked-files=all")
    git_dirty = bool(git_status)
    git_worktree_sha256 = hashlib.sha256(git_status.encode("utf-8")).hexdigest()
    fingerprint = build_eval_fingerprint(
        git_commit=git_commit,
        git_dirty=git_dirty,
        git_worktree_sha256=git_worktree_sha256,
        asset_manifest_sha256=asset_manifest["manifest_sha256"],
        config_sha256=config_summary["config_sha256"],
    )
    return {
        "suite": suite,
        "git_commit": git_commit,
        "git_branch": _git_output("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": git_dirty,
        "git_worktree_sha256": git_worktree_sha256,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "app_version": config.app_version,
        "rag_model": config.effective_rag_model,
        "embedding_model": config.dashscope_embedding_model,
        "rag_top_k": config.rag_top_k,
        "rag_hybrid_search_enabled": config.rag_hybrid_search_enabled,
        "rag_rerank_enabled": config.rag_rerank_enabled,
        "rag_retrieval_fusion_strategy": config.rag_retrieval_fusion_strategy,
        "rag_max_l2_distance": config.rag_max_l2_distance,
        "rag_min_lexical_trust_score": config.rag_min_lexical_trust_score,
        "aiops_mock_fallback_enabled": config.aiops_mock_fallback_enabled,
        "aiops_replanner_llm_enabled": config.aiops_replanner_llm_enabled,
        "api_auth_enabled": config.api_auth_enabled,
        "asset_manifest": asset_manifest,
        "asset_manifest_sha256": asset_manifest["manifest_sha256"],
        "config_summary": config_summary["values"],
        "config_sha256": config_summary["config_sha256"],
        "evaluation_fingerprint": fingerprint,
        "artifact_status": {
            "stale": False,
            "reasons": [],
            "generated_fingerprint": fingerprint,
            "current_fingerprint": fingerprint,
        },
    }


def collect_asset_manifest(root: Path = KNOWLEDGE_BASE_DIR) -> dict[str, Any]:
    """Return a stable hash manifest for all indexed knowledge assets."""
    files: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_ASSET_SUFFIXES:
                continue
            content = path.read_bytes()
            files.append(
                {
                    "path": path.relative_to(REPO_ROOT).as_posix(),
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "root": root.relative_to(REPO_ROOT).as_posix()
        if root.is_relative_to(REPO_ROOT)
        else str(root),
        "file_count": len(files),
        "files": files,
        "manifest_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def collect_config_summary() -> dict[str, Any]:
    """Return a non-secret configuration snapshot and its stable hash."""
    values = {
        "app_version": config.app_version,
        "rag_model": config.effective_rag_model,
        "embedding_model": config.dashscope_embedding_model,
        "rag_top_k": config.rag_top_k,
        "rag_hybrid_search_enabled": config.rag_hybrid_search_enabled,
        "rag_rerank_enabled": config.rag_rerank_enabled,
        "rag_retrieval_fusion_strategy": config.rag_retrieval_fusion_strategy,
        "rag_max_l2_distance": config.rag_max_l2_distance,
        "rag_min_lexical_trust_score": config.rag_min_lexical_trust_score,
        "chunk_max_size": config.chunk_max_size,
        "chunk_overlap": config.chunk_overlap,
        "index_allowed_roots": config.index_allowed_roots,
        "aiops_storage_backend": config.aiops_storage_backend,
        "aiops_mock_fallback_enabled": config.aiops_mock_fallback_enabled,
        "aiops_replanner_llm_enabled": config.aiops_replanner_llm_enabled,
        "api_auth_enabled": config.api_auth_enabled,
    }
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "values": values,
        "config_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def build_eval_fingerprint(
    *,
    git_commit: str,
    git_dirty: bool,
    git_worktree_sha256: str,
    asset_manifest_sha256: str,
    config_sha256: str,
) -> str:
    """Build the provenance fingerprint used to detect stale artifacts."""
    payload = {
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "git_worktree_sha256": git_worktree_sha256,
        "asset_manifest_sha256": asset_manifest_sha256,
        "config_sha256": config_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assess_eval_artifact_staleness(run: dict[str, Any] | None) -> dict[str, Any]:
    """Compare an artifact's provenance with the current repository state."""
    payload = run if isinstance(run, dict) else {}
    environment = payload.get("environment")
    if not isinstance(environment, dict):
        environment = payload
    current = collect_eval_environment(suite=str(environment.get("suite") or "artifact_check"))
    generated_fingerprint = str(environment.get("evaluation_fingerprint") or "")
    reasons: list[str] = []
    if not generated_fingerprint:
        reasons.append("missing_evaluation_fingerprint")
    if str(environment.get("git_commit") or "") != current["git_commit"]:
        reasons.append("git_commit_changed")
    if bool(environment.get("git_dirty")) != bool(current["git_dirty"]):
        reasons.append("git_dirty_state_changed")
    if str(environment.get("git_worktree_sha256") or "") != current["git_worktree_sha256"]:
        reasons.append("git_worktree_changed")
    if str(environment.get("asset_manifest_sha256") or "") != current["asset_manifest_sha256"]:
        reasons.append("knowledge_assets_changed")
    if str(environment.get("config_sha256") or "") != current["config_sha256"]:
        reasons.append("evaluation_config_changed")
    if generated_fingerprint and generated_fingerprint != current["evaluation_fingerprint"]:
        reasons.append("evaluation_fingerprint_changed")
    return {
        "stale": bool(reasons),
        "reasons": list(dict.fromkeys(reasons)),
        "generated_fingerprint": generated_fingerprint,
        "current_fingerprint": current["evaluation_fingerprint"],
    }


def provenance_markdown_lines(environment: dict[str, Any]) -> list[str]:
    """Render compact provenance lines shared by Markdown eval artifacts."""
    artifact_status = environment.get("artifact_status") or {}
    return [
        f"- Git commit: `{environment.get('git_commit', '')}`",
        f"- Dirty worktree: `{bool(environment.get('git_dirty'))}`",
        f"- Worktree state: `{environment.get('git_worktree_sha256', '')}`",
        f"- Knowledge assets: `{environment.get('asset_manifest_sha256', '')}`",
        f"- Eval config: `{environment.get('config_sha256', '')}`",
        f"- Artifact stale: `{bool(artifact_status.get('stale'))}`",
    ]


def _git_output(*args: str) -> str:
    """Return a git value without failing evals outside a git checkout."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
