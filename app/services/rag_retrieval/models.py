"""Internal retrieval types used by the RAG pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.documents import Document


@dataclass(frozen=True, slots=True)
class RetrievalOptions:
    """Validated retrieval controls resolved from configuration and call arguments."""

    top_k: int
    candidate_k: int
    max_distance: float
    min_lexical_score: float
    hybrid_search_enabled: bool
    rerank_enabled: bool
    fusion_strategy: str
    metadata_filter: dict[str, Any]
    metadata_filter_expr: str | None


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """Normalized input for one retrieval pipeline execution."""

    query: str
    options: RetrievalOptions


@dataclass(frozen=True, slots=True)
class BackendError:
    """Captured backend failure without exposing policy decisions."""

    backend: str
    detail: str = ""
    error_type: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.detail)


@dataclass(slots=True)
class BackendResult:
    """Raw results and failure state returned by both retrieval backends."""

    vector_results: list[tuple[Document, float | None]] = field(default_factory=list)
    lexical_results: list[tuple[Document, float]] = field(default_factory=list)
    vector_error: BackendError = field(default_factory=lambda: BackendError("vector"))
    lexical_error: BackendError = field(default_factory=lambda: BackendError("lexical"))
    stage_timings: dict[str, float] = field(
        default_factory=lambda: {
            "vector_search_ms": 0.0,
            "lexical_search_ms": 0.0,
            "fusion_rerank_ms": 0.0,
        }
    )
