"""Public compatibility entry point for structured RAG retrieval."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.lexical_index_service import lexical_index_service
from app.services.rag_retrieval.service import (
    retrieve_structured_knowledge as _retrieve_structured_knowledge,
)


def retrieve_structured_knowledge(
    query: str,
    *,
    top_k: int | None = None,
    max_distance: float | None = None,
    metadata_filter: dict[str, Any] | None = None,
    hybrid_search_enabled: bool | None = None,
    rerank_enabled: bool | None = None,
    fusion_strategy: str | None = None,
    vector_store: Any | None = None,
    vector_store_provider: Callable[[], Any] | None = None,
    lexical_index: Any | None = None,
) -> dict[str, Any]:
    """Preserve the historical retrieval call signature."""
    return _retrieve_structured_knowledge(
        query,
        top_k=top_k,
        max_distance=max_distance,
        metadata_filter=metadata_filter,
        hybrid_search_enabled=hybrid_search_enabled,
        rerank_enabled=rerank_enabled,
        fusion_strategy=fusion_strategy,
        vector_store=vector_store,
        vector_store_provider=vector_store_provider,
        lexical_index=lexical_index or lexical_index_service,
    )


__all__ = ["retrieve_structured_knowledge"]
