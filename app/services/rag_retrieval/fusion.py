"""Weighted and RRF fusion, reranking, and relevance pruning."""

from __future__ import annotations

from typing import Any

from app.services.rag_retrieval.candidates import (
    compute_lexical_score,
    deduplicate_candidates,
    extract_retrieval_terms,
    normalize_vector_distance,
)
from app.services.rag_retrieval.intent import (
    _required_sources_from_preferences,
    infer_retrieval_preferences,
    retrieval_intent_multiplier,
)
from app.services.rag_retrieval.selection import (
    select_diverse_sources,
    select_heading_coverage,
    select_required_sources,
)
from app.services.rag_retrieval.validation import (
    normalize_fusion_strategy,
)


def rerank_retrieval_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    top_k: int,
    hybrid_search_enabled: bool,
    rerank_enabled: bool,
    fusion_strategy: str | None = None,
    prune_low_relevance: bool = True,
) -> list[dict[str, Any]]:
    """Blend vector ranking with lexical signals and return final ordered chunks."""
    # The public top_k is capped at 100, but hybrid recall can legitimately
    # rerank up to 500 internal candidates.
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise ValueError("top_k 必须是正整数")
    if not candidates:
        return []

    query_terms = extract_retrieval_terms(query)
    retrieval_preferences = infer_retrieval_preferences(query)
    deduped = deduplicate_candidates(candidates)
    normalized_fusion_strategy = normalize_fusion_strategy(fusion_strategy)
    for index, chunk in enumerate(deduped, 1):
        metadata = dict(chunk.get("metadata") or {})
        has_vector_signal = (
            str(metadata.get("_retrieval_source") or "") in {"vector", "hybrid"}
            or metadata.get("_vector_score") is not None
        )
        has_lexical_signal = (
            str(metadata.get("_retrieval_source") or "") in {"lexical", "hybrid"}
            or metadata.get("_lexical_score") is not None
            or metadata.get("_lexical_rank") is not None
        )
        lexical_score = (
            compute_lexical_score(query_terms, chunk)
            if hybrid_search_enabled and has_lexical_signal
            else 0.0
        )
        vector_distance = chunk.get("score") if has_vector_signal else None
        vector_score = normalize_vector_distance(vector_distance) if vector_distance is not None else 0.0
        base_rank_score = 1 / max(index, 1)
        weighted_score = _weighted_fusion_score(
            vector_score=vector_score,
            lexical_score=lexical_score,
            base_rank_score=base_rank_score,
            fusion_enabled=rerank_enabled or hybrid_search_enabled,
        )
        rrf_score = _rrf_fusion_score(metadata, base_rank=index)
        raw_rerank_score = rrf_score if normalized_fusion_strategy == "rrf" else weighted_score
        intent_multiplier = (
            retrieval_intent_multiplier(chunk, retrieval_preferences)
            if normalized_fusion_strategy == "weighted"
            else 1.0
        )
        rerank_score = raw_rerank_score * intent_multiplier
        chunk["lexical_score"] = round(lexical_score, 4)
        chunk["vector_score"] = round(vector_score, 4)
        chunk["rerank_score"] = round(rerank_score, 4)
        chunk["rrf_score"] = round(rrf_score, 4)
        chunk["intent_multiplier"] = round(intent_multiplier, 4)
        chunk["fusion_strategy"] = normalized_fusion_strategy
        chunk["retrieval_signals"] = {
            "vector_score": chunk["vector_score"],
            "lexical_score": chunk["lexical_score"],
            "rerank_score": chunk["rerank_score"],
            "rrf_score": chunk["rrf_score"],
            "vector_rank": metadata.get("_vector_rank"),
            "lexical_rank": metadata.get("_lexical_rank"),
            "base_rank": index,
            "intent_multiplier": chunk["intent_multiplier"],
            "fusion_strategy": normalized_fusion_strategy,
        }

    if rerank_enabled or hybrid_search_enabled:
        deduped.sort(
            key=lambda item: (
                -float(item.get("rerank_score") or 0.0),
                _distance_sort_key(item.get("score")),
                str(item.get("source_file") or ""),
                str(item.get("chunk_id") or ""),
            )
        )

    required_sources = _required_sources_from_preferences(retrieval_preferences)
    if required_sources:
        deduped = select_required_sources(
            deduped,
            required_sources=required_sources,
            top_k=top_k,
        )
    elif normalized_fusion_strategy == "weighted":
        deduped = select_heading_coverage(
            deduped,
            query=query,
            top_k=top_k,
        )
        if bool(retrieval_preferences.get("require_source_diversity")):
            deduped = select_diverse_sources(deduped, top_k=top_k)

    selected = (
        prune_low_relevance_candidates(deduped, top_k=top_k)
        if prune_low_relevance and not required_sources
        else deduped[:top_k]
    )
    for rank, chunk in enumerate(selected, 1):
        chunk["rank"] = rank
    return selected


def _distance_sort_key(value: Any) -> float:
    """Use lower-is-better ordering for raw vector distances."""
    try:
        distance = float(value)
    except (TypeError, ValueError):
        return float("inf")
    return distance if distance >= 0 else float("inf")



def _weighted_fusion_score(
    *,
    vector_score: float,
    lexical_score: float,
    base_rank_score: float,
    fusion_enabled: bool,
) -> float:
    if not fusion_enabled:
        return base_rank_score
    return (0.55 * vector_score) + (0.35 * lexical_score) + (0.10 * base_rank_score)


def prune_low_relevance_candidates(
    candidates: list[dict[str, Any]],
    *,
    top_k: int,
    relative_floor: float = 0.70,
) -> list[dict[str, Any]]:
    """Avoid padding Top-K with materially weaker contexts."""
    limited = candidates[:top_k]
    if len(limited) <= 1:
        return limited
    best_score = float(limited[0].get("rerank_score") or 0.0)
    if best_score <= 0:
        return limited
    selected = [
        item
        for item in limited
        if float(item.get("rerank_score") or 0.0) >= best_score * relative_floor
    ]
    return selected or limited[:1]


def _rrf_fusion_score(
    metadata: dict[str, Any],
    *,
    base_rank: int,
    k: int = 60,
) -> float:
    ranks = [
        _coerce_rank(metadata.get("_vector_rank")),
        _coerce_rank(metadata.get("_lexical_rank")),
    ]
    observed_ranks = [rank for rank in ranks if rank is not None]
    if not observed_ranks:
        observed_ranks.append(max(base_rank, 1))
    return sum(1 / (k + rank) for rank in observed_ranks)


def _coerce_rank(value: Any) -> int | None:
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return None
    return rank if rank > 0 else None
