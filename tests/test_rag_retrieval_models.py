"""Contract tests for internal retrieval pipeline state."""

from app.services.rag_retrieval.models import (
    BackendError,
    BackendResult,
    RetrievalOptions,
    RetrievalRequest,
)


def test_internal_retrieval_types_keep_public_payload_explicit() -> None:
    options = RetrievalOptions(
        top_k=2,
        candidate_k=6,
        max_distance=1.0,
        min_lexical_score=0.1,
        hybrid_search_enabled=True,
        rerank_enabled=True,
        fusion_strategy="weighted",
        metadata_filter={},
        metadata_filter_expr=None,
    )
    request = RetrievalRequest(query="Redis timeout", options=options)
    backend = BackendResult(vector_error=BackendError("vector"))

    assert request.options.candidate_k == 6
    assert not backend.vector_error.failed
