"""Shared environment metadata for reproducible offline evaluation reports."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any
from uuid import uuid4

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
EVIDENCE_LEVELS = {
    "offline_fixture",
    "local_live",
    "controlled_fault",
    "production",
}
PROMPT_SOURCE_PATHS = [
    REPO_ROOT / "app" / "agent" / "aiops" / "planner.py",
    REPO_ROOT / "app" / "agent" / "aiops" / "replanner.py",
    REPO_ROOT / "app" / "agent" / "aiops" / "execution_fallbacks.py",
    REPO_ROOT / "app" / "services" / "aiops_prompt_builder.py",
    REPO_ROOT / "app" / "services" / "rag_agent_service.py",
    REPO_ROOT / "app" / "services" / "rag_answer_policy.py",
]


def collect_eval_environment(
    *,
    suite: str,
    evidence_level: str = "offline_fixture",
    run_id: str | None = None,
    execution_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return non-secret runtime metadata attached to every eval artifact."""
    if evidence_level not in EVIDENCE_LEVELS:
        raise ValueError(
            f"Unsupported evidence_level={evidence_level}; supported={sorted(EVIDENCE_LEVELS)}"
        )
    asset_manifest = collect_asset_manifest()
    config_summary = collect_config_summary()
    dependency_manifest = collect_dependency_manifest()
    prompt_manifest = collect_prompt_manifest()
    git_commit = _git_output("rev-parse", "HEAD")
    worktree = collect_worktree_state()
    git_dirty = bool(worktree["dirty"])
    git_worktree_sha256 = str(worktree["worktree_sha256"])
    fingerprint = build_eval_fingerprint(
        git_commit=git_commit,
        git_dirty=git_dirty,
        git_worktree_sha256=git_worktree_sha256,
        asset_manifest_sha256=asset_manifest["manifest_sha256"],
        config_sha256=config_summary["config_sha256"],
        dependency_manifest_sha256=dependency_manifest["manifest_sha256"],
        prompt_manifest_sha256=prompt_manifest["manifest_sha256"],
    )
    resolved_run_id = str(run_id or f"eval-{uuid4().hex}")
    identity = dict(execution_identity or {})
    return {
        "suite": suite,
        "run_id": resolved_run_id,
        "evidence_level": evidence_level,
        "evidence_levels_supported": sorted(EVIDENCE_LEVELS),
        "git_commit": git_commit,
        "git_branch": _git_output("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": git_dirty,
        "git_worktree_sha256": git_worktree_sha256,
        "git_tracked_diff_sha256": worktree["tracked_diff_sha256"],
        "git_untracked_manifest_sha256": worktree["untracked_manifest_sha256"],
        "git_changed_path_count": worktree["changed_path_count"],
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": collect_machine_summary(),
        "dependency_manifest": dependency_manifest,
        "dependency_manifest_sha256": dependency_manifest["manifest_sha256"],
        "prompt_manifest": prompt_manifest,
        "prompt_manifest_sha256": prompt_manifest["manifest_sha256"],
        "prompt_version": prompt_manifest["version"],
        "baseline_eligible": bool(git_commit and not git_dirty),
        "baseline_ineligible_reasons": (
            []
            if git_commit and not git_dirty
            else ["dirty_worktree"]
            if git_dirty
            else ["missing_git"]
        ),
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
        "execution_identity": {
            "configured_model": config.effective_rag_model,
            "configured_embedding_model": config.dashscope_embedding_model,
            "actual_model": str(identity.get("actual_model") or config.effective_rag_model),
            "actual_embedding_model": str(
                identity.get("actual_embedding_model") or config.dashscope_embedding_model
            ),
            "provider": str(identity.get("provider") or ""),
            "execution_path": str(identity.get("execution_path") or "deterministic"),
            "fallback_used": bool(identity.get("fallback_used", False)),
            "model_calls": identity.get("model_calls", []),
            **{
                key: value
                for key, value in identity.items()
                if key
                not in {
                    "actual_model",
                    "actual_embedding_model",
                    "provider",
                    "execution_path",
                    "fallback_used",
                    "model_calls",
                }
            },
        },
        "artifact_status": {
            "stale": False,
            "reasons": [],
            "generated_fingerprint": fingerprint,
            "current_fingerprint": fingerprint,
        },
    }


def collect_worktree_state(root: Path = REPO_ROOT) -> dict[str, Any]:
    """Hash tracked diff bytes and untracked file contents, not only status paths."""
    status = _git_bytes(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    tracked_diff = _git_bytes(root, "diff", "--binary", "HEAD", "--")
    untracked_raw = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    untracked_paths = [
        item.decode("utf-8", errors="surrogateescape")
        for item in untracked_raw.split(b"\0")
        if item
    ]
    untracked_files = []
    for relative in sorted(untracked_paths):
        path = root / relative
        if not path.is_file():
            continue
        content = path.read_bytes()
        untracked_files.append(
            {
                "path": Path(relative).as_posix(),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    untracked_encoded = json.dumps(
        untracked_files,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    tracked_diff_sha256 = hashlib.sha256(tracked_diff).hexdigest()
    untracked_manifest_sha256 = hashlib.sha256(untracked_encoded).hexdigest()
    combined = json.dumps(
        {
            "status_sha256": hashlib.sha256(status).hexdigest(),
            "tracked_diff_sha256": tracked_diff_sha256,
            "untracked_manifest_sha256": untracked_manifest_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "dirty": bool(status),
        "changed_path_count": len([item for item in status.split(b"\0") if item]),
        "tracked_diff_sha256": tracked_diff_sha256,
        "untracked_manifest_sha256": untracked_manifest_sha256,
        "untracked_file_count": len(untracked_files),
        "worktree_sha256": hashlib.sha256(combined).hexdigest(),
    }


def collect_dependency_manifest() -> dict[str, Any]:
    """Return installed package versions plus the lock-file hash."""

    def package_name(distribution: metadata.Distribution) -> str:
        return str(distribution.metadata["Name"] or "").lower()

    packages = sorted(
        {
            package_name(distribution): distribution.version
            for distribution in metadata.distributions()
            if package_name(distribution)
        }.items()
    )
    lock_path = REPO_ROOT / "uv.lock"
    lock_sha256 = hashlib.sha256(lock_path.read_bytes()).hexdigest() if lock_path.exists() else ""
    encoded = json.dumps(packages, ensure_ascii=False, separators=(",", ":"))
    return {
        "package_count": len(packages),
        "packages": [{"name": name, "version": version} for name, version in packages],
        "lock_file": "uv.lock" if lock_path.exists() else "",
        "lock_sha256": lock_sha256,
        "manifest_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def collect_prompt_manifest() -> dict[str, Any]:
    """Hash prompt-bearing source files so results identify prompt revisions."""
    files = []
    for path in PROMPT_SOURCE_PATHS:
        if not path.exists():
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
    manifest_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return {
        "version": f"prompt-{manifest_sha256[:12]}",
        "file_count": len(files),
        "files": files,
        "manifest_sha256": manifest_sha256,
    }


def collect_machine_summary() -> dict[str, Any]:
    """Return non-secret machine characteristics needed to interpret timings."""
    return {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count() or 0,
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
        "root": (
            root.relative_to(REPO_ROOT).as_posix() if root.is_relative_to(REPO_ROOT) else str(root)
        ),
        "file_count": len(files),
        "files": files,
        "manifest_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def collect_dataset_provenance(path: str | Path, *, case_count: int) -> dict[str, Any]:
    """Return immutable identity fields for one evaluation case file."""
    dataset_path = Path(path)
    if not dataset_path.exists() or not dataset_path.is_file():
        return {
            "path": str(dataset_path),
            "sha256": "",
            "size_bytes": 0,
            "modified_at_ns": 0,
            "case_count": case_count,
        }
    content = dataset_path.read_bytes()
    stat = dataset_path.stat()
    return {
        "path": str(dataset_path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "modified_at_ns": stat.st_mtime_ns,
        "case_count": case_count,
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
        "rag_model_timeout_seconds": config.rag_model_timeout_seconds,
        "rag_model_max_retries": config.rag_model_max_retries,
        "rag_model_retry_delay_seconds": config.rag_model_retry_delay_seconds,
        "ragas_eval_model": config.effective_ragas_eval_model,
        "ragas_eval_embedding_model": config.effective_ragas_eval_embedding_model,
        "ragas_min_faithfulness": config.ragas_min_faithfulness,
        "ragas_min_response_relevancy": config.ragas_min_response_relevancy,
        "ragas_min_id_context_precision": config.ragas_min_id_context_precision,
        "ragas_min_id_context_recall": config.ragas_min_id_context_recall,
        "ragas_min_oncall_actionability": config.ragas_min_oncall_actionability,
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
    dependency_manifest_sha256: str = "",
    prompt_manifest_sha256: str = "",
) -> str:
    """Build the provenance fingerprint used to detect stale artifacts."""
    payload = {
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "git_worktree_sha256": git_worktree_sha256,
        "asset_manifest_sha256": asset_manifest_sha256,
        "config_sha256": config_sha256,
        "dependency_manifest_sha256": dependency_manifest_sha256,
        "prompt_manifest_sha256": prompt_manifest_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assess_eval_artifact_staleness(
    run: dict[str, Any] | None,
    *,
    current_environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare an artifact's provenance with the current repository state."""
    payload = run if isinstance(run, dict) else {}
    environment = payload.get("environment")
    if not isinstance(environment, dict):
        environment = payload
    current = current_environment or collect_eval_environment(
        suite=str(environment.get("suite") or "artifact_check")
    )
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
    if (
        str(environment.get("dependency_manifest_sha256") or "")
        != current["dependency_manifest_sha256"]
    ):
        reasons.append("dependencies_changed")
    if str(environment.get("prompt_manifest_sha256") or "") != current["prompt_manifest_sha256"]:
        reasons.append("prompts_changed")
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
        f"- Evidence level: `{environment.get('evidence_level', 'unknown')}`",
        f"- Official baseline eligible: `{bool(environment.get('baseline_eligible'))}`",
        f"- Prompt version: `{environment.get('prompt_version', '')}`",
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


def _git_bytes(root: Path, *args: str) -> bytes:
    """Return raw git output so binary diffs and NUL-separated paths hash correctly."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return b""
    return result.stdout if result.returncode == 0 else b""
