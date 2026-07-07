"""Shared environment metadata for reproducible offline evaluation reports."""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.config import config

REPO_ROOT = Path(__file__).resolve().parents[2]


def collect_eval_environment(*, suite: str) -> dict[str, Any]:
    """Return non-secret runtime metadata attached to every eval artifact."""
    return {
        "suite": suite,
        "git_commit": _git_output("rev-parse", "HEAD"),
        "git_branch": _git_output("rev-parse", "--abbrev-ref", "HEAD"),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "app_version": config.app_version,
        "rag_model": config.effective_rag_model,
        "embedding_model": config.dashscope_embedding_model,
        "rag_top_k": config.rag_top_k,
        "rag_hybrid_search_enabled": config.rag_hybrid_search_enabled,
        "rag_rerank_enabled": config.rag_rerank_enabled,
        "rag_max_l2_distance": config.rag_max_l2_distance,
        "rag_min_lexical_trust_score": config.rag_min_lexical_trust_score,
        "aiops_mock_fallback_enabled": config.aiops_mock_fallback_enabled,
        "aiops_replanner_llm_enabled": config.aiops_replanner_llm_enabled,
        "api_auth_enabled": config.api_auth_enabled,
    }


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
