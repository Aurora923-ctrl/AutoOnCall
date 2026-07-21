"""Pipeline orchestration for structured RAG retrieval."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from app.config import config
from app.services.policies.retrieval_policy import (
    ANSWER_WITH_CITATIONS,
    REFUSE_WITHOUT_TRUSTED_SOURCE,
    RETRIEVAL_FAILED,
    RETRIEVAL_FAILED_POLICY,
    RETRIEVAL_NO_ANSWER,
    RETRIEVAL_SUCCESS,
)
from app.services.rag_retrieval.backends import (
    _candidate_count,
    _search_with_optional_scores,
    _should_query_lexical_index,
    _validate_retrieval_k,
    build_degraded_retrieval_mode,
    build_public_lexical_error_message,
    build_public_vector_error_message,
    build_retrieval_mode,
    build_targeted_lexical_queries,
    build_vector_degraded_retrieval_mode,
    exact_entity_lexical_results,
    targeted_lexical_results,
)
from app.services.rag_retrieval.candidates import (
    disambiguate_citation_sources,
    document_to_retrieval_chunk,
    format_retrieval_results,
    is_stale_retrieval_source,
    merge_raw_retrieval_results,
    merge_targeted_lexical_results,
)
from app.services.rag_retrieval.fusion import (
    prune_low_relevance_candidates,
    rerank_retrieval_candidates,
)
from app.services.rag_retrieval.intent import (
    _required_sources_from_preferences,
    infer_retrieval_preferences,
)
from app.services.rag_retrieval.metadata import (
    build_milvus_metadata_expr,
    metadata_matches_filter,
    normalize_metadata_filter,
)
from app.services.rag_retrieval.models import (
    BackendError,
    BackendResult,
    RetrievalOptions,
    RetrievalRequest,
)
from app.services.rag_retrieval.selection import (
    build_retrieval_reason,
    enforce_source_coverage,
    is_trusted_retrieval_chunk,
    query_is_out_of_scope,
    select_required_sources,
)
from app.services.rag_retrieval.validation import normalize_fusion_strategy
from app.utils.log_safety import summarize_text_for_log

NO_TRUSTED_KNOWLEDGE = "\u672a\u627e\u5230\u53ef\u4fe1\u77e5\u8bc6\u6765\u6e90\u3002"
PUBLIC_RETRIEVAL_ERROR = (
    "\u77e5\u8bc6\u5e93\u68c0\u7d22\u6682\u4e0d\u53ef\u7528\uff0c"
    "\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u67e5\u770b\u670d\u52a1\u7aef\u65e5\u5fd7\u3002"
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
    lexical_index: Any,
) -> dict[str, Any]:
    """Retrieve trusted chunks while preserving the legacy public dictionary contract."""
    total_started = time.perf_counter()
    request: RetrievalRequest | None = None
    backends = BackendResult()
    counts = _PipelineCounts()
    try:
        request = _build_request(
            query,
            top_k=top_k,
            max_distance=max_distance,
            metadata_filter=metadata_filter,
            hybrid_search_enabled=hybrid_search_enabled,
            rerank_enabled=rerank_enabled,
            fusion_strategy=fusion_strategy,
        )
        backends = _retrieve_backends(
            request,
            vector_store=vector_store,
            vector_store_provider=vector_store_provider,
            lexical_index=lexical_index,
        )
        candidates = _prepare_candidates(request, backends, lexical_index, counts)
        trusted, rejected, required_sources, missing_sources = _apply_gates(
            request,
            candidates,
        )
        mode = _resolved_retrieval_mode(request.options, backends)
        return _build_outcome(
            request,
            backends,
            counts,
            trusted=trusted,
            rejected=rejected,
            required_sources=required_sources,
            missing_required_sources=missing_sources,
            retrieval_mode=mode,
            total_started=total_started,
        )
    except Exception as exc:
        logger.error(f"结构化知识检索失败: {exc}")
        return _build_failed_outcome(
            query=str(query or "").strip(),
            request=request,
            backends=backends,
            counts=counts,
            total_started=total_started,
            top_k=top_k,
            max_distance=max_distance,
            hybrid_search_enabled=hybrid_search_enabled,
            rerank_enabled=rerank_enabled,
            fusion_strategy=fusion_strategy,
        )


class _PipelineCounts:
    def __init__(self) -> None:
        self.merged = 0
        self.deduplicated = 0
        self.filtered_stale = 0
        self.filtered_metadata = 0


def _build_request(
    query: str,
    *,
    top_k: int | None,
    max_distance: float | None,
    metadata_filter: dict[str, Any] | None,
    hybrid_search_enabled: bool | None,
    rerank_enabled: bool | None,
    fusion_strategy: str | None,
) -> RetrievalRequest:
    safe_query = str(query or "").strip()
    if not safe_query:
        raise ValueError("query 不能为空")
    if len(safe_query) > 8000:
        raise ValueError("query 长度不能超过 8000")
    k = config.rag_top_k if top_k is None else top_k
    _validate_retrieval_k(k, label="top_k")
    threshold: Any = config.rag_max_l2_distance if max_distance is None else max_distance
    if isinstance(threshold, bool):
        raise ValueError("max_distance 必须是非负数")
    try:
        threshold = float(threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_distance 必须是非负数") from exc
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("max_distance 必须是非负有限数")
    if isinstance(hybrid_search_enabled, bool) is False and hybrid_search_enabled is not None:
        raise ValueError("hybrid_search_enabled 必须是布尔值")
    if isinstance(rerank_enabled, bool) is False and rerank_enabled is not None:
        raise ValueError("rerank_enabled 必须是布尔值")
    hybrid = (
        config.rag_hybrid_search_enabled
        if hybrid_search_enabled is None
        else hybrid_search_enabled
    )
    rerank = config.rag_rerank_enabled if rerank_enabled is None else rerank_enabled
    normalized_filter = normalize_metadata_filter(metadata_filter, strict=True)
    options = RetrievalOptions(
        top_k=k,
        candidate_k=_candidate_count(k) if hybrid or rerank else k,
        max_distance=threshold,
        min_lexical_score=config.rag_min_lexical_trust_score,
        hybrid_search_enabled=hybrid,
        rerank_enabled=rerank,
        fusion_strategy=normalize_fusion_strategy(fusion_strategy),
        metadata_filter=normalized_filter,
        metadata_filter_expr=build_milvus_metadata_expr(normalized_filter),
    )
    return RetrievalRequest(query=safe_query, options=options)


def _retrieve_backends(
    request: RetrievalRequest,
    *,
    vector_store: Any | None,
    vector_store_provider: Callable[[], Any] | None,
    lexical_index: Any,
) -> BackendResult:
    result = BackendResult()
    provider = vector_store_provider
    vector_started = time.perf_counter()
    try:
        store = vector_store or (provider() if provider is not None else None)
        if store is None:
            raise RuntimeError("vector store provider returned no store")
        result.vector_results = _search_with_optional_scores(
            store,
            request.query,
            request.options.candidate_k,
            expr=request.options.metadata_filter_expr,
        )
    except Exception as exc:
        result.vector_error = BackendError("vector", str(exc), type(exc).__name__)
        if vector_store is not None or not request.options.hybrid_search_enabled:
            raise
        logger.warning(
            "向量检索不可用，降级使用本地词法索引: {}, error_type={}",
            summarize_text_for_log(request.query, label="query"),
            type(exc).__name__,
        )
    finally:
        result.stage_timings["vector_search_ms"] = _elapsed_ms(vector_started)

    if request.options.hybrid_search_enabled and (
        result.vector_error.failed
        or _should_query_lexical_index(vector_store, result.vector_results)
    ):
        _run_lexical_search(request, lexical_index, result)
    if request.options.hybrid_search_enabled and vector_store is None:
        _run_targeted_lexical_search(request, lexical_index, result)
    return result


def _run_lexical_search(
    request: RetrievalRequest,
    lexical_index: Any,
    result: BackendResult,
) -> None:
    started = time.perf_counter()
    try:
        result.lexical_results = list(
            lexical_index.search(
                request.query,
                top_k=request.options.candidate_k,
                metadata_filter=request.options.metadata_filter,
            )
        )
    except Exception as exc:
        if result.vector_error.failed:
            raise RuntimeError(
                "向量检索失败且词法索引降级也失败: "
                f"vector_error={result.vector_error.detail}; lexical_error={exc}"
            ) from exc
        if not result.vector_results:
            raise
        result.lexical_error = BackendError("lexical", str(exc), type(exc).__name__)
        logger.warning(
            "词法检索不可用，继续使用向量候选: {}, error_type={}",
            summarize_text_for_log(request.query, label="query"),
            type(exc).__name__,
        )
    finally:
        result.stage_timings["lexical_search_ms"] = _elapsed_ms(started)


def _run_targeted_lexical_search(
    request: RetrievalRequest,
    lexical_index: Any,
    result: BackendResult,
) -> None:
    started = time.perf_counter()
    try:
        if build_targeted_lexical_queries(request.query):
            result.lexical_results = merge_targeted_lexical_results(
                result.lexical_results,
                targeted_lexical_results(
                    lexical_index,
                    request.query,
                    metadata_filter=request.options.metadata_filter,
                ),
            )
        result.lexical_results = merge_targeted_lexical_results(
            result.lexical_results,
            exact_entity_lexical_results(
                lexical_index,
                request.query,
                top_k=request.options.candidate_k,
                metadata_filter=request.options.metadata_filter,
            ),
        )
    except Exception as exc:
        logger.warning(
            "定向词法扩展失败，保留已有候选: {}, error_type={}",
            summarize_text_for_log(request.query, label="query"),
            type(exc).__name__,
        )
    finally:
        result.stage_timings["lexical_search_ms"] += _elapsed_ms(started)


def _prepare_candidates(
    request: RetrievalRequest,
    backends: BackendResult,
    lexical_index: Any,
    counts: _PipelineCounts,
) -> list[dict[str, Any]]:
    raw_results = merge_raw_retrieval_results(
        backends.vector_results,
        backends.lexical_results,
    )
    counts.merged = len(raw_results)
    counts.deduplicated = max(
        len(backends.vector_results) + len(backends.lexical_results) - counts.merged,
        0,
    )
    candidates = disambiguate_citation_sources(
        [
            document_to_retrieval_chunk(document, score=score, rank=rank)
            for rank, (document, score) in enumerate(raw_results, 1)
        ]
    )
    for chunk in candidates:
        _validate_candidate_provenance(chunk)
    before = len(candidates)
    candidates = [
        chunk
        for chunk in candidates
        if not is_stale_retrieval_source(chunk, lexical_index=lexical_index)
    ]
    counts.filtered_stale = before - len(candidates)
    before = len(candidates)
    if request.options.metadata_filter:
        candidates = [
            chunk
            for chunk in candidates
            if metadata_matches_filter(chunk["metadata"], request.options.metadata_filter)
        ]
    counts.filtered_metadata = before - len(candidates)
    before = len(candidates)
    started = time.perf_counter()
    try:
        candidates = rerank_retrieval_candidates(
            request.query,
            candidates,
            top_k=max(len(candidates), request.options.top_k),
            hybrid_search_enabled=request.options.hybrid_search_enabled,
            rerank_enabled=request.options.rerank_enabled,
            fusion_strategy=request.options.fusion_strategy,
            prune_low_relevance=False,
        )
    finally:
        backends.stage_timings["fusion_rerank_ms"] = _elapsed_ms(started)
    counts.deduplicated += before - len(candidates)
    return candidates


def _apply_gates(
    request: RetrievalRequest,
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str], set[str]]:
    trusted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for chunk in candidates:
        is_trusted = is_trusted_retrieval_chunk(
            chunk,
            max_distance=request.options.max_distance,
            min_lexical_score=request.options.min_lexical_score,
        )
        enriched = chunk | {
            "retrieval_reason": build_retrieval_reason(
                chunk,
                request.options.max_distance,
                min_lexical_score=request.options.min_lexical_score,
                trusted=is_trusted,
            )
        }
        (trusted if is_trusted else rejected).append(enriched)

    required = _required_sources_from_preferences(infer_retrieval_preferences(request.query))
    trusted_before_coverage = trusted
    trusted = select_required_sources(
        trusted,
        required_sources=required,
        top_k=request.options.top_k,
    )
    if request.options.fusion_strategy == "weighted" and not required:
        trusted = prune_low_relevance_candidates(trusted, top_k=request.options.top_k)
    else:
        trusted = trusted[: request.options.top_k]
    if not required and query_is_out_of_scope(request.query, trusted):
        trusted = []
    trusted, missing = enforce_source_coverage(trusted, required_sources=required)
    if missing:
        rejected.extend(
            chunk
            | {
                "retrieval_reason": (
                    "required source coverage missing after trust gate: "
                    + ", ".join(sorted(missing))
                )
            }
            for chunk in trusted_before_coverage
        )
    for rank, chunk in enumerate(trusted, 1):
        chunk["rank"] = rank
    return trusted, rejected, required, missing


def _validate_candidate_provenance(chunk: dict[str, Any]) -> None:
    """Mark malformed backend metadata unusable before ranking or generation."""
    metadata = chunk.get("metadata")
    if not isinstance(metadata, dict):
        chunk["metadata"] = {}
        metadata = {}
    source_file = chunk.get("source_file")
    chunk_id = chunk.get("chunk_id")
    content = chunk.get("content") or chunk.get("content_preview")
    source_id = chunk.get("source_id") or chunk.get("doc_id")
    retrieval_source = str(metadata.get("_retrieval_source") or "").strip().lower()
    valid_source = retrieval_source in {"vector", "lexical", "hybrid"}
    valid_text_identity = all(
        isinstance(value, str) and bool(value.strip())
        for value in (source_file, chunk_id, source_id, content)
    )
    normalized_source_file = source_file.strip() if isinstance(source_file, str) else ""
    normalized_chunk_id = chunk_id.strip() if isinstance(chunk_id, str) else ""
    normalized_source_id = source_id.strip() if isinstance(source_id, str) else ""
    normalized_content = content.strip() if isinstance(content, str) else ""
    valid_identity = bool(
        chunk.get("metadata_types_valid", True)
        and valid_text_identity
        and normalized_source_file != "未知来源"
        and normalized_chunk_id != "unknown"
        and normalized_source_id
        and normalized_content
        and valid_source
    )
    chunk["metadata_identity_valid"] = valid_identity
    if not valid_identity:
        chunk["retrieval_reason"] = "检索元数据、来源类型或正文缺失，已拒绝"


def _resolved_retrieval_mode(options: RetrievalOptions, backends: BackendResult) -> str:
    if backends.vector_error.failed:
        return build_degraded_retrieval_mode(options.rerank_enabled, options.fusion_strategy)
    if backends.lexical_error.failed:
        return build_vector_degraded_retrieval_mode(
            options.rerank_enabled,
            options.fusion_strategy,
        )
    return build_retrieval_mode(
        options.hybrid_search_enabled,
        options.rerank_enabled,
        options.fusion_strategy,
    )


def _build_outcome(
    request: RetrievalRequest,
    backends: BackendResult,
    counts: _PipelineCounts,
    *,
    trusted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    required_sources: set[str],
    missing_required_sources: set[str],
    retrieval_mode: str,
    total_started: float,
) -> dict[str, Any]:
    success = bool(trusted)
    payload = _base_payload(request, backends, retrieval_mode)
    payload.update(
        {
            "status": RETRIEVAL_SUCCESS if success else RETRIEVAL_NO_ANSWER,
            "no_answer_rejected": not success,
            "answer_policy": (
                ANSWER_WITH_CITATIONS if success else REFUSE_WITHOUT_TRUSTED_SOURCE
            ),
            "retrieval_results": trusted,
            "rejected_results": rejected,
            "required_sources": sorted(required_sources),
            "missing_required_sources": sorted(missing_required_sources),
            "generation_allowlist": [
                {
                    "source_file": str(item.get("source_file") or ""),
                    "chunk_id": str(item.get("chunk_id") or ""),
                }
                for item in trusted
            ],
            "observability": _observability(
                request,
                backends,
                counts,
                total_started,
                trusted_count=len(trusted),
                rejected_count=len(rejected),
                retrieval_mode=retrieval_mode,
            ),
            "summary": (
                f"检索到 {len(trusted)} 条可信知识来源"
                if success
                else NO_TRUSTED_KNOWLEDGE
            ),
            "content": format_retrieval_results(trusted) if success else NO_TRUSTED_KNOWLEDGE,
        }
    )
    return payload


def _base_payload(
    request: RetrievalRequest,
    backends: BackendResult,
    retrieval_mode: str,
) -> dict[str, Any]:
    options = request.options
    return {
        "query": request.query,
        "source": "rag",
        "top_k": options.top_k,
        "candidate_k": options.candidate_k,
        "max_l2_distance": options.max_distance,
        "min_lexical_trust_score": options.min_lexical_score,
        "retrieval_mode": retrieval_mode,
        "fusion_strategy": options.fusion_strategy,
        "retrieval_degraded": backends.vector_error.failed or backends.lexical_error.failed,
        "vector_error_message": build_public_vector_error_message(
            backends.vector_error.detail
        ),
        "vector_error_type": backends.vector_error.error_type,
        "vector_error_detail": backends.vector_error.detail,
        "lexical_error_message": build_public_lexical_error_message(
            backends.lexical_error.detail
        ),
        "lexical_error_type": backends.lexical_error.error_type,
        "lexical_error_detail": backends.lexical_error.detail,
        "vector_candidate_count": len(backends.vector_results),
        "lexical_candidate_count": len(backends.lexical_results),
        "metadata_filter": options.metadata_filter,
        "metadata_filter_expr": options.metadata_filter_expr,
    }


def _observability(
    request: RetrievalRequest,
    backends: BackendResult,
    counts: _PipelineCounts,
    total_started: float,
    *,
    trusted_count: int,
    rejected_count: int,
    retrieval_mode: str,
) -> dict[str, Any]:
    vector_count = len(backends.vector_results)
    lexical_count = len(backends.lexical_results)
    return {
        "stages": {
            "embedding_ms": "not_observed",
            "milvus_search_ms": "not_observed",
            "vector_search_ms": backends.stage_timings.get("vector_search_ms", 0.0),
            "lexical_search_ms": backends.stage_timings.get("lexical_search_ms", 0.0),
            "fusion_rerank_ms": backends.stage_timings.get("fusion_rerank_ms", 0.0),
            "retrieval_total_ms": _elapsed_ms(total_started),
        },
        "counts": {
            "vector_candidate_count": vector_count,
            "lexical_candidate_count": lexical_count,
            "retriever_hit_count": vector_count + lexical_count,
            "candidate_count": counts.merged,
            "merged_candidate_count": counts.merged,
            "deduplicated_count": counts.deduplicated,
            "filtered_stale_count": counts.filtered_stale,
            "filtered_metadata_count": counts.filtered_metadata,
            "trusted_count": trusted_count,
            "rejected_count": rejected_count,
        },
        "runtime": {
            "retrieval_mode": retrieval_mode,
            "embedding_model": config.dashscope_embedding_model,
            "reranker_model": (
                "rule-weighted" if request.options.rerank_enabled else "disabled"
            ),
            "collection_version": "not_observed",
        },
        "limitations": [
            (
                "LangChain Milvus similarity_search combines query embedding and Milvus search; "
                "only their combined vector_search_ms is observed."
            )
        ],
    }


def _build_failed_outcome(
    *,
    query: str,
    request: RetrievalRequest | None,
    backends: BackendResult,
    counts: _PipelineCounts,
    total_started: float,
    top_k: int | None,
    max_distance: float | None,
    hybrid_search_enabled: bool | None,
    rerank_enabled: bool | None,
    fusion_strategy: str | None,
) -> dict[str, Any]:
    k = config.rag_top_k if top_k is None else top_k
    threshold = config.rag_max_l2_distance if max_distance is None else max_distance
    hybrid = (
        config.rag_hybrid_search_enabled
        if hybrid_search_enabled is None
        else hybrid_search_enabled
    )
    rerank = config.rag_rerank_enabled if rerank_enabled is None else rerank_enabled
    strategy = normalize_fusion_strategy(fusion_strategy)
    mode = build_retrieval_mode(hybrid, rerank, strategy)
    if request is not None:
        payload = _base_payload(request, backends, mode)
        observability = _observability(
            request,
            backends,
            counts,
            total_started,
            trusted_count=0,
            rejected_count=0,
            retrieval_mode=mode,
        )
    else:
        payload = {
            "query": query,
            "source": "rag",
            "top_k": k,
            "candidate_k": 0,
            "max_l2_distance": threshold,
            "min_lexical_trust_score": config.rag_min_lexical_trust_score,
            "retrieval_mode": mode,
            "fusion_strategy": strategy,
            "retrieval_degraded": False,
            "vector_error_message": build_public_vector_error_message(
                backends.vector_error.detail
            ),
            "vector_error_type": backends.vector_error.error_type,
            "vector_error_detail": backends.vector_error.detail,
            "lexical_error_message": build_public_lexical_error_message(
                backends.lexical_error.detail
            ),
            "lexical_error_type": backends.lexical_error.error_type,
            "lexical_error_detail": backends.lexical_error.detail,
            "vector_candidate_count": len(backends.vector_results),
            "lexical_candidate_count": len(backends.lexical_results),
            "metadata_filter": {},
            "metadata_filter_expr": None,
        }
        fallback_request = RetrievalRequest(
            query=query,
            options=RetrievalOptions(
                top_k=int(k) if isinstance(k, int) and not isinstance(k, bool) else 0,
                candidate_k=0,
                max_distance=(
                    float(threshold)
                    if isinstance(threshold, int | float) and not isinstance(threshold, bool)
                    else 0.0
                ),
                min_lexical_score=config.rag_min_lexical_trust_score,
                hybrid_search_enabled=hybrid,
                rerank_enabled=rerank,
                fusion_strategy=strategy,
                metadata_filter={},
                metadata_filter_expr=None,
            ),
        )
        observability = _observability(
            fallback_request,
            backends,
            counts,
            total_started,
            trusted_count=0,
            rejected_count=0,
            retrieval_mode=mode,
        )
    payload.update(
        {
            "status": RETRIEVAL_FAILED,
            "no_answer_rejected": False,
            "answer_policy": RETRIEVAL_FAILED_POLICY,
            "retrieval_results": [],
            "rejected_results": [],
            "observability": observability,
            "summary": PUBLIC_RETRIEVAL_ERROR,
            "content": PUBLIC_RETRIEVAL_ERROR,
        }
    )
    return payload


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)
