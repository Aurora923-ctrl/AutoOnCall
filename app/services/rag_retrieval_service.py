"""Public compatibility entry point for structured RAG retrieval."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.lexical_index_service import lexical_index_service
from app.services.rag_retrieval.service import (
    retrieve_structured_knowledge as _retrieve_structured_knowledge,
)
from app.services.vector_store_manager import vector_store_manager


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
    runtime_default_path = vector_store is None and vector_store_provider is None
    provider = vector_store_provider
    if provider is None and vector_store is None:
        provider = vector_store_manager.get_vector_store
    return _retrieve_structured_knowledge(
        query,
        top_k=top_k,
        max_distance=max_distance,
        metadata_filter=metadata_filter,
        hybrid_search_enabled=hybrid_search_enabled,
        rerank_enabled=rerank_enabled,
        fusion_strategy=fusion_strategy,
        vector_store=vector_store,
        vector_store_provider=provider,
        lexical_index=lexical_index or lexical_index_service,
        allow_lexical_fallback=not runtime_default_path,
        vector_backend_label="milvus" if runtime_default_path else "injected",
    )


__all__ = ["retrieve_structured_knowledge"]
