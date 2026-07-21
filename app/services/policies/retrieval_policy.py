"""Canonical RAG outcomes, answer policies, trust gates, and fallback modes."""

from __future__ import annotations

import math
from typing import Any, Literal

RetrievalStatus = Literal["success", "no_answer", "failed"]
AnswerPolicy = Literal[
    "answer_with_citations",
    "refuse_without_trusted_source",
    "refuse_without_citation",
    "retrieval_failed",
]

RETRIEVAL_SUCCESS: RetrievalStatus = "success"
RETRIEVAL_NO_ANSWER: RetrievalStatus = "no_answer"
RETRIEVAL_FAILED: RetrievalStatus = "failed"
ANSWER_WITH_CITATIONS: AnswerPolicy = "answer_with_citations"
REFUSE_WITHOUT_TRUSTED_SOURCE: AnswerPolicy = "refuse_without_trusted_source"
REFUSE_WITHOUT_CITATION: AnswerPolicy = "refuse_without_citation"
RETRIEVAL_FAILED_POLICY: AnswerPolicy = "retrieval_failed"

VECTOR_RETRIEVAL_SOURCES = frozenset({"vector", "hybrid"})
LEXICAL_RETRIEVAL_SOURCES = frozenset({"lexical", "hybrid"})


def is_trusted_l2_distance(score: Any, max_distance: float) -> bool:
    """Return whether a vector result passes the configured L2 trust gate."""
    if score is None:
        return False
    try:
        value = float(score)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and 0 <= value <= max_distance


def retrieval_mode(
    *,
    hybrid_enabled: bool,
    rerank_enabled: bool,
    fusion_strategy: str,
    degraded_backend: Literal["", "vector", "lexical"] = "",
) -> str:
    """Return the stable observability label for a retrieval strategy."""
    rrf = fusion_strategy == "rrf"
    if degraded_backend == "vector":
        if not rerank_enabled:
            return "lexical_degraded"
        return "lexical_degraded_rrf_rerank" if rrf else "lexical_degraded_rerank"
    if degraded_backend == "lexical":
        if not rerank_enabled:
            return "vector_degraded"
        return "vector_degraded_rrf_rerank" if rrf else "vector_degraded_rerank"
    if hybrid_enabled and rerank_enabled:
        return "hybrid_vector_lexical_rrf_rerank" if rrf else "hybrid_vector_lexical_rerank"
    if hybrid_enabled:
        return "hybrid_vector_lexical_rrf" if rrf else "hybrid_vector_lexical"
    if rerank_enabled:
        return "vector_rrf_rerank" if rrf else "vector_rerank"
    return "vector"
