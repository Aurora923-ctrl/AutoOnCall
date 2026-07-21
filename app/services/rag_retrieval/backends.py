"""Vector and lexical backend access with stable degradation labels."""

from __future__ import annotations

from typing import Any, cast

from langchain_core.documents import Document

from app.services.policies.retrieval_policy import retrieval_mode
from app.services.rag_retrieval.candidates import _coerce_score
from app.services.rag_retrieval.intent import (
    build_targeted_lexical_queries,
    extract_exact_retrieval_entities,
)
from app.services.rag_retrieval.validation import (
    candidate_count,
    normalize_fusion_strategy,
    validate_retrieval_k,
)


def targeted_lexical_results(
    lexical_index: Any,
    query: str,
    *,
    metadata_filter: dict[str, Any] | None = None,
) -> list[tuple[Document, float]]:
    """Recall explicitly requested sources or runbook sections through the existing index."""
    results: list[tuple[Document, float]] = []
    for source_file, expanded_query in build_targeted_lexical_queries(query).items():
        source_filter = dict(metadata_filter or {})
        existing_source = source_filter.get("_file_name")
        if existing_source:
            if isinstance(existing_source, list):
                if source_file not in {str(item) for item in existing_source}:
                    continue
            elif str(existing_source) != source_file:
                continue
        source_filter["_file_name"] = source_file
        results.extend(
            cast(
                list[tuple[Document, float]],
                lexical_index.search(
                    expanded_query,
                    top_k=2,
                    metadata_filter=source_filter,
                ),
            )
        )
    return results


def exact_entity_lexical_results(
    lexical_index: Any,
    query: str,
    *,
    top_k: int,
    metadata_filter: dict[str, Any] | None = None,
) -> list[tuple[Document, float]]:
    """Recall exact incident IDs, release versions, and primary-key entities."""
    entities = extract_exact_retrieval_entities(query)
    search = getattr(lexical_index, "search_exact_entities", None)
    if not entities or not callable(search):
        return []
    return cast(
        list[tuple[Document, float]],
        search(
            query,
            entities=entities,
            top_k=top_k,
            metadata_filter=metadata_filter,
        ),
    )


def build_retrieval_mode(
    hybrid_search_enabled: bool,
    rerank_enabled: bool,
    fusion_strategy: str | None = None,
) -> str:
    """Return a stable retrieval-mode label for observability and eval reports."""
    return retrieval_mode(
        hybrid_enabled=hybrid_search_enabled,
        rerank_enabled=rerank_enabled,
        fusion_strategy=normalize_fusion_strategy(fusion_strategy),
    )


def build_degraded_retrieval_mode(
    rerank_enabled: bool,
    fusion_strategy: str | None = None,
) -> str:
    """Return the retrieval mode used when vector search falls back to lexical only."""
    return retrieval_mode(
        hybrid_enabled=True,
        rerank_enabled=rerank_enabled,
        fusion_strategy=normalize_fusion_strategy(fusion_strategy),
        degraded_backend="vector",
    )


def build_vector_degraded_retrieval_mode(
    rerank_enabled: bool,
    fusion_strategy: str | None = None,
) -> str:
    """Return the retrieval mode used when lexical search fails but vector search succeeds."""
    return retrieval_mode(
        hybrid_enabled=True,
        rerank_enabled=rerank_enabled,
        fusion_strategy=normalize_fusion_strategy(fusion_strategy),
        degraded_backend="lexical",
    )


def build_public_vector_error_message(error_detail: str) -> str:
    """Return a frontend-safe vector retrieval error summary."""
    if not error_detail:
        return ""
    return "向量检索暂不可用，已降级使用本地词法索引。"


def build_public_lexical_error_message(error_detail: str) -> str:
    """Return a frontend-safe lexical retrieval error summary."""
    if not error_detail:
        return ""
    return "词法检索暂不可用，已降级使用向量检索结果。"


def _should_query_lexical_index(
    injected_vector_store: Any | None,
    vector_results: list[tuple[Document, float | None]],
) -> bool:
    """Decide whether to add global lexical-index candidates.

    Production retrieval uses the default vector store and should benefit from
    hybrid recall. Tests and offline evaluators often inject a small vector
    store to make threshold behavior deterministic; in that case only fall back
    to the global lexical index when the injected store returns no candidates.
    """
    return injected_vector_store is None or not vector_results


def _search_with_optional_scores(
    vector_store: Any,
    query: str,
    top_k: int,
    *,
    expr: str | None = None,
) -> list[tuple[Document, float | None]]:
    """Use scored search when available and fall back to plain similarity search."""
    if hasattr(vector_store, "similarity_search_with_score"):
        scored = _call_similarity_search_with_score(vector_store, query, top_k, expr)
        return [(document, _coerce_score(score)) for document, score in scored]

    docs = _call_similarity_search(vector_store, query, top_k, expr)
    return [(document, None) for document in docs]


def _call_similarity_search_with_score(
    vector_store: Any,
    query: str,
    top_k: int,
    expr: str | None,
) -> list[tuple[Document, Any]]:
    if expr:
        return cast(
            list[tuple[Document, Any]],
            vector_store.similarity_search_with_score(query, k=top_k, expr=expr),
        )
    return cast(
        list[tuple[Document, Any]],
        vector_store.similarity_search_with_score(query, k=top_k),
    )


def _call_similarity_search(
    vector_store: Any,
    query: str,
    top_k: int,
    expr: str | None,
) -> list[Document]:
    if expr:
        return cast(list[Document], vector_store.similarity_search(query, k=top_k, expr=expr))
    return cast(list[Document], vector_store.similarity_search(query, k=top_k))


def _candidate_count(top_k: int) -> int:
    return candidate_count(top_k)


def _validate_retrieval_k(value: Any, *, label: str) -> int:
    return validate_retrieval_k(value, label=label)
