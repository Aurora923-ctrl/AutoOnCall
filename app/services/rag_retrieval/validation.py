"""Shared retrieval option validation without backend/fusion coupling."""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.config import config

MAX_RETRIEVAL_TOP_K = 100
MAX_RETRIEVAL_CANDIDATES = 500


def normalize_fusion_strategy(value: str | None = None) -> str:
    """Normalize the configured fusion strategy with the legacy fallback."""
    strategy = str(value or config.rag_retrieval_fusion_strategy or "weighted").strip().lower()
    if strategy not in {"weighted", "rrf"}:
        logger.warning("不支持的 RAG fusion strategy，回退 weighted: strategy={}", strategy)
        return "weighted"
    return strategy


def candidate_count(top_k: int) -> int:
    validate_retrieval_k(top_k, label="top_k")
    multiplier = max(int(config.rag_hybrid_candidate_multiplier or 1), 1)
    return min(max(top_k, top_k * multiplier), MAX_RETRIEVAL_CANDIDATES)


def validate_retrieval_k(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} 必须是正整数")
    if value > MAX_RETRIEVAL_TOP_K:
        raise ValueError(f"{label} 不能超过 {MAX_RETRIEVAL_TOP_K}")
    return int(value)
