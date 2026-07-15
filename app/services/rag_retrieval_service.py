"""Structured retrieval helpers for trustworthy RAG citations."""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from langchain_core.documents import Document
from loguru import logger

from app.config import config
from app.services.lexical_index_service import lexical_index_service
from app.services.vector_store_manager import vector_store_manager
from app.utils.log_safety import summarize_text_for_log

NO_TRUSTED_KNOWLEDGE = "未找到可信知识来源。"
PUBLIC_RETRIEVAL_ERROR = "知识库检索暂不可用，请稍后重试或查看服务端日志。"
CITATION_INSTRUCTION = (
    "引用要求: 仅基于下列可信知识回答；回答末尾列出引用来源，格式为 source_file + chunk_id。"
)
METADATA_FILTER_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    """Retrieve knowledge chunks with source metadata, scores, and rejection details."""
    total_started = time.perf_counter()
    safe_query = str(query or "").strip()
    k = top_k or config.rag_top_k
    threshold = config.rag_max_l2_distance if max_distance is None else max_distance
    lexical_threshold = config.rag_min_lexical_trust_score
    hybrid_enabled = (
        config.rag_hybrid_search_enabled if hybrid_search_enabled is None else hybrid_search_enabled
    )
    rerank_on = config.rag_rerank_enabled if rerank_enabled is None else rerank_enabled
    normalized_fusion_strategy = normalize_fusion_strategy(fusion_strategy)
    candidate_k = _candidate_count(k) if hybrid_enabled or rerank_on else k
    expr = build_milvus_metadata_expr(metadata_filter)
    resolved_lexical_index = lexical_index or lexical_index_service
    resolved_vector_store_provider = vector_store_provider or vector_store_manager.get_vector_store

    vector_error_detail = ""
    vector_error_type = ""
    stage_timings = {
        "vector_search_ms": 0.0,
        "lexical_search_ms": 0.0,
        "fusion_rerank_ms": 0.0,
    }

    try:
        vector_results: list[tuple[Document, float | None]] = []
        vector_started = time.perf_counter()
        try:
            store = vector_store or resolved_vector_store_provider()
            vector_results = _search_with_optional_scores(store, safe_query, candidate_k, expr=expr)
        except Exception as exc:
            vector_error_detail = str(exc)
            vector_error_type = type(exc).__name__
            if vector_store is not None or not hybrid_enabled:
                raise
            logger.warning(
                "向量检索不可用，降级使用本地词法索引: {}, error_type={}",
                summarize_text_for_log(safe_query, label="query"),
                vector_error_type,
            )
        finally:
            stage_timings["vector_search_ms"] = _elapsed_ms(vector_started)

        lexical_results: list[tuple[Document, float]] = []
        if hybrid_enabled and (
            vector_error_detail or _should_query_lexical_index(vector_store, vector_results)
        ):
            lexical_started = time.perf_counter()
            try:
                lexical_results = cast(
                    list[tuple[Document, float]],
                    resolved_lexical_index.search(
                        safe_query,
                        top_k=candidate_k,
                        metadata_filter=normalize_metadata_filter(metadata_filter),
                    ),
                )
            except Exception as exc:
                if vector_error_detail:
                    raise RuntimeError(
                        "向量检索失败且词法索引降级也失败: "
                        f"vector_error={vector_error_detail}; lexical_error={exc}"
                    ) from exc
                raise
            finally:
                stage_timings["lexical_search_ms"] = _elapsed_ms(lexical_started)

        if hybrid_enabled and vector_store is None:
            lexical_started = time.perf_counter()
            try:
                lexical_results = merge_targeted_lexical_results(
                    lexical_results,
                    targeted_lexical_results(
                        resolved_lexical_index,
                        safe_query,
                        metadata_filter=normalize_metadata_filter(metadata_filter),
                    ),
                )
            finally:
                stage_timings["lexical_search_ms"] += _elapsed_ms(lexical_started)

        retrieval_mode = (
            build_degraded_retrieval_mode(rerank_on, normalized_fusion_strategy)
            if vector_error_detail
            else build_retrieval_mode(hybrid_enabled, rerank_on, normalized_fusion_strategy)
        )
        vector_error_message = build_public_vector_error_message(vector_error_detail)
        raw_results = merge_raw_retrieval_results(vector_results, lexical_results)
        candidates = [
            document_to_retrieval_chunk(document, score=score, rank=rank)
            for rank, (document, score) in enumerate(raw_results, 1)
        ]
        candidates = [chunk for chunk in candidates if not is_stale_retrieval_source(chunk)]
        if metadata_filter:
            candidates = [
                chunk
                for chunk in candidates
                if metadata_matches_filter(chunk["metadata"], metadata_filter)
            ]
        rerank_started = time.perf_counter()
        try:
            candidates = rerank_retrieval_candidates(
                safe_query,
                candidates,
                top_k=k,
                hybrid_search_enabled=hybrid_enabled,
                rerank_enabled=rerank_on,
                fusion_strategy=normalized_fusion_strategy,
                prune_low_relevance=False,
            )
        finally:
            stage_timings["fusion_rerank_ms"] = _elapsed_ms(rerank_started)
        trusted = []
        rejected = []
        for chunk in candidates:
            if is_trusted_retrieval_chunk(
                chunk,
                max_distance=threshold,
                min_lexical_score=lexical_threshold,
            ):
                trusted.append(
                    chunk
                    | {
                        "retrieval_reason": build_retrieval_reason(
                            chunk,
                            threshold,
                            min_lexical_score=lexical_threshold,
                            trusted=True,
                        ),
                    }
                )
            else:
                rejected.append(
                    chunk
                    | {
                        "retrieval_reason": build_retrieval_reason(
                            chunk,
                            threshold,
                            min_lexical_score=lexical_threshold,
                            trusted=False,
                        ),
                    }
                )

        required_sources = _required_sources_from_preferences(
            infer_retrieval_preferences(safe_query)
        )
        if normalized_fusion_strategy == "weighted" and not required_sources:
            trusted = prune_low_relevance_candidates(trusted, top_k=k)

        if not trusted:
            return {
                "status": "no_answer",
                "query": safe_query,
                "source": "rag",
                "top_k": k,
                "candidate_k": candidate_k,
                "max_l2_distance": threshold,
                "min_lexical_trust_score": lexical_threshold,
                "retrieval_mode": retrieval_mode,
                "fusion_strategy": normalized_fusion_strategy,
                "retrieval_degraded": bool(vector_error_detail),
                "vector_error_message": vector_error_message,
                "vector_error_type": vector_error_type,
                "vector_error_detail": vector_error_detail,
                "vector_candidate_count": len(vector_results),
                "lexical_candidate_count": len(lexical_results),
                "metadata_filter": normalize_metadata_filter(metadata_filter),
                "metadata_filter_expr": expr,
                "no_answer_rejected": True,
                "answer_policy": "refuse_without_trusted_source",
                "retrieval_results": [],
                "rejected_results": rejected,
                "observability": build_retrieval_observability(
                    stage_timings,
                    total_started=total_started,
                    vector_candidate_count=len(vector_results),
                    lexical_candidate_count=len(lexical_results),
                    trusted_count=0,
                    rejected_count=len(rejected),
                    retrieval_mode=retrieval_mode,
                ),
                "summary": NO_TRUSTED_KNOWLEDGE,
                "content": NO_TRUSTED_KNOWLEDGE,
            }

        return {
            "status": "success",
            "query": safe_query,
            "source": "rag",
            "top_k": k,
            "candidate_k": candidate_k,
            "max_l2_distance": threshold,
            "min_lexical_trust_score": lexical_threshold,
            "retrieval_mode": retrieval_mode,
            "fusion_strategy": normalized_fusion_strategy,
            "retrieval_degraded": bool(vector_error_detail),
            "vector_error_message": vector_error_message,
            "vector_error_type": vector_error_type,
            "vector_error_detail": vector_error_detail,
            "vector_candidate_count": len(vector_results),
            "lexical_candidate_count": len(lexical_results),
            "metadata_filter": normalize_metadata_filter(metadata_filter),
            "metadata_filter_expr": expr,
            "no_answer_rejected": False,
            "answer_policy": "answer_with_citations",
            "retrieval_results": trusted,
            "rejected_results": rejected,
            "observability": build_retrieval_observability(
                stage_timings,
                total_started=total_started,
                vector_candidate_count=len(vector_results),
                lexical_candidate_count=len(lexical_results),
                trusted_count=len(trusted),
                rejected_count=len(rejected),
                retrieval_mode=retrieval_mode,
            ),
            "summary": f"检索到 {len(trusted)} 条可信知识来源",
            "content": format_retrieval_results(trusted),
        }
    except Exception as exc:
        logger.error(f"结构化知识检索失败: {exc}")
        return {
            "status": "failed",
            "query": safe_query,
            "source": "rag",
            "top_k": k,
            "candidate_k": candidate_k,
            "max_l2_distance": threshold,
            "min_lexical_trust_score": lexical_threshold,
            "retrieval_mode": build_retrieval_mode(
                hybrid_enabled,
                rerank_on,
                normalized_fusion_strategy,
            ),
            "fusion_strategy": normalized_fusion_strategy,
            "retrieval_degraded": False,
            "vector_error_message": build_public_vector_error_message(vector_error_detail),
            "vector_error_type": vector_error_type,
            "vector_error_detail": vector_error_detail,
            "vector_candidate_count": 0,
            "lexical_candidate_count": 0,
            "metadata_filter": normalize_metadata_filter(metadata_filter),
            "metadata_filter_expr": expr,
            "no_answer_rejected": False,
            "answer_policy": "retrieval_failed",
            "retrieval_results": [],
            "rejected_results": [],
            "observability": build_retrieval_observability(
                stage_timings,
                total_started=total_started,
                vector_candidate_count=0,
                lexical_candidate_count=0,
                trusted_count=0,
                rejected_count=0,
                retrieval_mode=build_retrieval_mode(
                    hybrid_enabled,
                    rerank_on,
                    normalized_fusion_strategy,
                ),
            ),
            "summary": PUBLIC_RETRIEVAL_ERROR,
            "content": PUBLIC_RETRIEVAL_ERROR,
            "error_message": PUBLIC_RETRIEVAL_ERROR,
        }


def build_retrieval_observability(
    stage_timings: dict[str, float],
    *,
    total_started: float,
    vector_candidate_count: int,
    lexical_candidate_count: int,
    trusted_count: int,
    rejected_count: int,
    retrieval_mode: str,
) -> dict[str, Any]:
    """Build honest stage observations without inventing hidden backend timings."""
    return {
        "stages": {
            "embedding_ms": "not_observed",
            "milvus_search_ms": "not_observed",
            "vector_search_ms": stage_timings.get("vector_search_ms", 0.0),
            "lexical_search_ms": stage_timings.get("lexical_search_ms", 0.0),
            "fusion_rerank_ms": stage_timings.get("fusion_rerank_ms", 0.0),
            "retrieval_total_ms": _elapsed_ms(total_started),
        },
        "counts": {
            "vector_candidate_count": vector_candidate_count,
            "lexical_candidate_count": lexical_candidate_count,
            "candidate_count": vector_candidate_count + lexical_candidate_count,
            "trusted_count": trusted_count,
            "rejected_count": rejected_count,
        },
        "runtime": {
            "retrieval_mode": retrieval_mode,
            "embedding_model": config.dashscope_embedding_model,
            "reranker_model": "rule-weighted" if config.rag_rerank_enabled else "disabled",
            "collection_version": "not_observed",
        },
        "limitations": [
            (
                "LangChain Milvus similarity_search combines query embedding and Milvus search; "
                "only their combined vector_search_ms is observed."
            )
        ],
    }


def document_to_retrieval_chunk(
    document: Document,
    *,
    score: float | None,
    rank: int,
) -> dict[str, Any]:
    """Convert a LangChain Document into a stable retrieval chunk payload."""
    metadata = dict(document.metadata or {})
    source = str(metadata.get("_source") or metadata.get("source") or "")
    source_file = str(
        metadata.get("_file_name") or metadata.get("source_file") or source or "未知来源"
    )
    content = str(document.page_content or "")
    heading_path = build_heading_path(metadata)
    chunk_id = str(
        metadata.get("_chunk_id")
        or metadata.get("chunk_id")
        or metadata.get("id")
        or _stable_chunk_id(source_file, heading_path, content)
    )
    doc_id = str(metadata.get("_doc_id") or metadata.get("doc_id") or source or source_file)

    return {
        "rank": rank,
        "doc_id": doc_id,
        "source_file": source_file,
        "source_path": source,
        "heading_path": heading_path,
        "chunk_id": chunk_id,
        "score": score,
        "content_preview": content[: config.rag_content_preview_chars],
        "content": content,
        "metadata": metadata,
    }


def is_stale_retrieval_source(chunk: dict[str, Any]) -> bool:
    """Return True when an indexed source was superseded but failed to re-index."""
    source_path = str(chunk.get("source_path") or "")
    if not source_path:
        return False
    try:
        return lexical_index_service.is_source_stale(source_path)
    except Exception as exc:
        logger.warning(f"检查陈旧 RAG source 失败: source={source_path}, error={exc}")
        return True


def merge_raw_retrieval_results(
    vector_results: list[tuple[Document, float | None]],
    lexical_results: list[tuple[Document, float]],
) -> list[tuple[Document, float | None]]:
    """Merge vector and lexical candidates while preserving both score signals."""
    merged: dict[tuple[str, str], tuple[Document, float | None, int]] = {}
    for position, (document, score) in enumerate(vector_results, 1):
        key = _document_identity(document)
        metadata = dict(document.metadata or {})
        metadata["_vector_score"] = _coerce_score(score)
        metadata["_vector_rank"] = position
        metadata.setdefault("_retrieval_source", "vector")
        merged[key] = (
            Document(page_content=document.page_content, metadata=metadata),
            score,
            position,
        )

    base_position = len(vector_results)
    for offset, (document, lexical_score) in enumerate(lexical_results, 1):
        key = _document_identity(document)
        if key in merged:
            existing_document, existing_score, position = merged[key]
            metadata = dict(existing_document.metadata or {})
            metadata["_lexical_score"] = lexical_score
            metadata["_lexical_rank"] = offset
            metadata["_retrieval_source"] = "hybrid"
            merged[key] = (
                Document(page_content=existing_document.page_content, metadata=metadata),
                existing_score,
                position,
            )
            continue
        metadata = dict(document.metadata or {})
        metadata["_lexical_score"] = lexical_score
        metadata["_lexical_rank"] = offset
        metadata["_retrieval_source"] = "lexical"
        merged[key] = (
            Document(page_content=document.page_content, metadata=metadata),
            None,
            base_position + offset,
        )

    return [
        (document, score)
        for document, score, _position in sorted(
            merged.values(),
            key=lambda item: (
                item[2],
                float(item[1]) if item[1] is not None else 0.0,
                item[0].metadata.get("_chunk_id", ""),
            ),
        )
    ]


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
        if existing_source and str(existing_source) != source_file:
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


def build_targeted_lexical_queries(query: str) -> dict[str, str]:
    """Build conservative query expansions only for explicit retrieval subgoals."""
    lowered = str(query or "").lower()
    preferences = infer_retrieval_preferences(query)
    targeted: dict[str, str] = {}
    required_sources = _required_sources_from_preferences(preferences)
    source_hints = {
        "official_redis_clients.md": "maxclients maximum concurrent connected clients accepting connections",
        "redis_postmortem.pdf": "incident window connected_clients blocked_clients maxclients postmortem",
        "official_kubernetes_debug_pods.md": "debug pods kubectl describe pod state events running readiness",
        "official_kubernetes_debug_services.md": "debug service selector endpointslice backend pods",
        "official_loki_troubleshoot_ingest.md": (
            "loki discarded samples bytes monitoring ingestion errors"
        ),
        "official_prometheus_alerting_practices.md": (
            "alert symptoms user-visible pain what to alert on"
        ),
    }
    for source_file in required_sources:
        targeted[source_file] = f"{query} {source_hints.get(source_file, source_file)}"

    runbook_source = next(
        (
            source
            for term, source in {
                "cpu": "cpu_high_usage.md",
                "oom": "memory_high_usage.md",
                "oomkilled": "memory_high_usage.md",
                "内存": "memory_high_usage.md",
                "inode": "disk_high_usage.md",
                "磁盘": "disk_high_usage.md",
            }.items()
            if term in lowered
        ),
        "",
    )
    raw_heading_terms = preferences.get("preferred_heading_terms")
    heading_terms = set(raw_heading_terms) if isinstance(raw_heading_terms, set) else set()
    if runbook_source and heading_terms:
        targeted[runbook_source] = f"{query} {' '.join(sorted(heading_terms))}"
    return targeted


def merge_targeted_lexical_results(
    base_results: list[tuple[Document, float]],
    targeted_results: list[tuple[Document, float]],
) -> list[tuple[Document, float]]:
    """Merge targeted lexical hits without duplicating existing chunks."""
    merged = list(base_results)
    seen = {_document_identity(document) for document, _score in merged}
    for document, score in targeted_results:
        identity = _document_identity(document)
        if identity in seen:
            continue
        merged.append((document, score))
        seen.add(identity)
    return merged


def build_heading_path(metadata: dict[str, Any]) -> str:
    """Build a breadcrumb-like heading path from document metadata."""
    if metadata.get("heading_path"):
        return str(metadata["heading_path"])
    headers = [str(metadata[key]).strip() for key in ("h1", "h2", "h3") if metadata.get(key)]
    return " > ".join(headers)


def is_trusted_l2_distance(score: Any, max_distance: float) -> bool:
    """Return True when a result score is within the accepted L2 distance threshold."""
    if score is None:
        return False
    try:
        return float(score) <= max_distance
    except (TypeError, ValueError):
        return False


def is_trusted_retrieval_chunk(
    chunk: dict[str, Any],
    *,
    max_distance: float,
    min_lexical_score: float,
) -> bool:
    """Return True when either vector distance or lexical score crosses its own trust gate."""
    metadata = dict(chunk.get("metadata") or {})
    retrieval_source = str(metadata.get("_retrieval_source") or "")
    has_vector_signal = (
        retrieval_source in {"vector", "hybrid"} or metadata.get("_vector_score") is not None
    )
    if has_vector_signal and is_trusted_l2_distance(chunk.get("score"), max_distance):
        return True
    if retrieval_source in {"lexical", "hybrid"}:
        return _trusted_lexical_score(chunk, min_lexical_score)
    return False


def build_retrieval_reason(
    chunk: dict[str, Any],
    max_distance: float,
    *,
    min_lexical_score: float,
    trusted: bool,
) -> str:
    """Explain why a retrieval chunk was accepted or rejected."""
    metadata = dict(chunk.get("metadata") or {})
    retrieval_source = str(metadata.get("_retrieval_source") or "")
    score = chunk.get("score")
    if retrieval_source in {"vector", "hybrid"} or metadata.get("_vector_score") is not None:
        if score is None:
            vector_reason = "检索后端未返回距离分数"
        else:
            try:
                score_value = float(score)
                relation = "小于等于" if score_value <= max_distance else "大于"
                vector_reason = f"L2 distance {score_value:.4f} {relation} 阈值 {max_distance:.4f}"
            except (TypeError, ValueError):
                vector_reason = f"距离分数不可解析: {score}"
        if trusted and is_trusted_l2_distance(score, max_distance):
            return vector_reason
        if retrieval_source != "hybrid":
            return vector_reason

    lexical_score = _coerce_score(chunk.get("lexical_score"))
    if lexical_score is None:
        return "词法召回缺少可信分数" if not trusted else "词法召回通过可信阈值"
    relation = "大于等于" if lexical_score >= min_lexical_score else "小于"
    return f"lexical score {lexical_score:.4f} {relation} 阈值 {min_lexical_score:.4f}"


def format_retrieval_results(results: list[dict[str, Any]]) -> str:
    """Format structured retrieval chunks as LLM-readable context."""
    if not results:
        return NO_TRUSTED_KNOWLEDGE

    parts: list[str] = [CITATION_INSTRUCTION]
    for item in results:
        score = item.get("score")
        score_text = "未知" if score is None else f"{float(score):.4f}"
        heading = str(item.get("heading_path") or "").strip()
        source = str(item.get("source_file") or "未知来源")
        chunk_id = str(item.get("chunk_id") or "")
        content = str(item.get("content") or item.get("content_preview") or "").strip()
        metadata = dict(item.get("metadata") or {})

        lines = [
            f"【可信知识 {item.get('rank', len(parts) + 1)}】",
            f"source_file: {source}",
            f"chunk_id: {chunk_id}",
            f"score: {score_text}",
        ]
        if metadata.get("page_number"):
            lines.append(f"page_number: {metadata.get('page_number')}")
        if metadata.get("sheet_name"):
            lines.append(f"sheet_name: {metadata.get('sheet_name')}")
        if metadata.get("row_number"):
            lines.append(f"row_number: {metadata.get('row_number')}")
        if metadata.get("primary_key"):
            lines.append(f"primary_key: {metadata.get('primary_key')}")
        if heading:
            lines.append(f"标题路径: {heading}")
        lines.extend(["内容:", content])
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def documents_to_context(docs: list[Document]) -> str:
    """Format existing Document lists through the structured retrieval representation."""
    chunks = [
        document_to_retrieval_chunk(document, score=None, rank=rank)
        for rank, document in enumerate(docs, 1)
    ]
    return format_retrieval_results(chunks)


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
        lexical_score = compute_lexical_score(query_terms, chunk) if hybrid_search_enabled else 0.0
        vector_score = normalize_vector_distance(chunk.get("score")) if has_vector_signal else 0.0
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
                float(item.get("score") or 0.0),
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

    selected = (
        prune_low_relevance_candidates(deduped, top_k=top_k)
        if prune_low_relevance and not required_sources
        else deduped[:top_k]
    )
    for rank, chunk in enumerate(selected, 1):
        chunk["rank"] = rank
    return selected


def normalize_fusion_strategy(value: str | None = None) -> str:
    """Return the supported retrieval fusion strategy without changing safe defaults."""
    strategy = str(value or config.rag_retrieval_fusion_strategy or "weighted").strip().lower()
    return strategy if strategy in {"weighted", "rrf"} else "weighted"


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


def infer_retrieval_preferences(query: str) -> dict[str, set[str] | bool]:
    """Infer conservative source preferences from explicit query wording."""
    lowered = str(query or "").lower()
    preferred_doc_types: set[str] = set()
    preferred_extensions: set[str] = set()
    preferred_heading_terms: set[str] = set()
    required_sources: set[str] = set()

    if any(
        term in lowered for term in {"postmortem", "复盘", "事故复盘", "历史事故", "事故时间线"}
    ):
        preferred_doc_types.add("pdf")
        preferred_extensions.add(".pdf")
    if any(term in lowered for term in {"incident-window", "事故窗口", "故障窗口"}):
        preferred_doc_types.add("pdf")
        preferred_extensions.add(".pdf")
    if any(
        term in lowered for term in {"wiki", "runbook", "知识库", "如何排查", "应先查", "先查什么"}
    ):
        preferred_doc_types.add("html")
        preferred_extensions.update({".html", ".htm"})
    if any(term in lowered for term in {"审批边界", "容量边界"}):
        preferred_doc_types.add("html")
        preferred_extensions.update({".html", ".htm"})
    if "redis" in lowered and any(term in lowered for term in {"两个可信来源", "哪两个"}):
        preferred_doc_types.update({"pdf", "html"})
        preferred_extensions.update({".pdf", ".html", ".htm"})
    if any(
        term in lowered
        for term in {
            "ticket",
            "工单",
            "历史记录",
            "历史案例",
            "inc-",
            "deploy_history",
            "部署历史",
            "版本记录",
        }
    ):
        preferred_doc_types.add("table")
    if "tickets.csv" in lowered or "inc-" in lowered:
        preferred_extensions.add(".csv")
    if (
        "tickets.xlsx" in lowered
        or "deploy_history" in lowered
        or "部署历史" in lowered
        or "版本记录" in lowered
        or re.search(r"\brc\d+\b", lowered)
    ):
        preferred_extensions.add(".xlsx")

    if any(term in lowered for term in {"证据", "取证", "如何收集", "怎样区分"}):
        preferred_heading_terms.update({"排查步骤", "常用命令", "相关工具命令"})
    if any(
        term in lowered
        for term in {
            "处置边界",
            "审批",
            "重启",
            "扩容",
            "限流",
            "回滚",
            "删除",
            "截断",
            "清理",
            "dry-run",
        }
    ):
        preferred_heading_terms.add("升级与审批")
    if "redis" in lowered and "官方" in lowered and any(
        term in lowered for term in {"复盘", "事故"}
    ):
        required_sources.update({"official_redis_clients.md", "redis_postmortem.pdf"})
    if (
        any(term in lowered for term in {"kubernetes", "k8s"})
        and "pod" in lowered
        and "service" in lowered
        and "endpointslice" in lowered
    ):
        required_sources.update(
            {
                "official_kubernetes_debug_pods.md",
                "official_kubernetes_debug_services.md",
            }
        )
    if (
        "loki" in lowered
        and "discarded" in lowered
        and any(term in lowered for term in {"告警", "alert"})
    ):
        required_sources.update(
            {
                "official_loki_troubleshoot_ingest.md",
                "official_prometheus_alerting_practices.md",
            }
        )

    specific_fault_query = any(
        term in lowered
        for term in {
            "redis",
            "mysql",
            "sql",
            "maxclients",
            "pool_waiting",
            "active_connections",
            "retry",
            "重试",
        }
    )
    generic_service_query = any(
        term in lowered for term in {"503", "5xx", "service unavailable", "服务不可用"}
    )
    preferred_source_terms = {
        term
        for term, aliases in {
            "redis": {
                "redis",
                "maxclients",
                "blocked_clients",
                "connected_clients",
                "空闲客户端",
                "客户端连接",
                "服务端关闭",
                "连接槽位",
            },
            "mysql": {
                "mysql",
                "sql",
                "pool_waiting",
                "active_connections",
                "慢查询",
                "慢 sql",
                "慢sql",
                "连接池",
                "数据库",
                "支付",
                "payment",
            },
            "kubernetes": {
                "kubernetes",
                "k8s",
                "pod",
                "service",
                "endpointslice",
                "clusterip",
                "容器",
            },
            "prometheus": {
                "prometheus",
                "promql",
                "alerting",
                "告警规则",
                "用户可见",
                "告警原则",
                "内部原因",
                "pending",
                "firing",
                "症状告警",
                "告警实践",
            },
            "loki": {
                "loki",
                "logql",
                "ingestion",
                "ingester",
                "日志查询",
                "日志摄取",
                "日志查询语言",
                "返回 400",
                "可观测性写入",
            },
            "cpu": {"cpu", "load", "线程", "火焰图"},
            "memory": {"memory", "oom", "oomkilled", "内存"},
            "disk": {"disk", "inode", "no space", "磁盘"},
            "service_unavailable": {"503", "5xx", "服务不可用", "无法访问", "接口全部失败"},
            "slow_response": {
                "slow",
                "latency",
                "p95",
                "响应延迟",
                "响应时间",
                "接口变慢",
                "外部接口",
                "下游",
            },
        }.items()
        if any(alias in lowered for alias in aliases)
    }
    dominant_source_terms = {
        term
        for term, aliases in {
            "redis": {
                "maxclients",
                "blocked_clients",
                "connected_clients",
                "空闲客户端",
                "客户端连接",
                "服务端关闭",
                "连接上限",
                "连接槽位",
                "连接数满",
                "新连接被拒绝",
                "客户端数",
                "客户端限制",
                "缓存节点",
            },
            "mysql": {
                "mysql",
                "sql",
                "pool_waiting",
                "active_connections",
                "慢查询",
                "慢 sql",
                "慢sql",
                "连接池",
                "数据库",
                "支付",
                "payment",
            },
            "kubernetes": {"kubernetes", "k8s", "endpointslice", "clusterip", "selector", "pod"},
            "prometheus": {
                "prometheus",
                "promql",
                "alerting rule",
                "告警规则",
                "用户可见",
                "告警原则",
                "内部原因",
                "pending",
                "firing",
                "症状告警",
                "告警实践",
            },
            "loki": {
                "loki",
                "logql",
                "ingestion",
                "ingester",
                "日志摄取",
                "日志查询语言",
                "返回 400",
                "可观测性写入",
            },
            "cpu": {"cpu", "load", "火焰图"},
            "memory": {"memory", "oom", "oomkilled", "内存"},
            "disk": {"disk", "inode", "no space", "磁盘"},
            "service_unavailable": {"503", "5xx", "服务不可用", "无法访问", "接口全部失败"},
            "slow_response": {"响应延迟", "响应时间", "接口变慢", "外部接口", "下游"},
        }.items()
        if any(alias in lowered for alias in aliases)
    }
    if "mysql" in dominant_source_terms or any(
        term in lowered for term in {"慢 sql", "慢sql", "pool_waiting", "数据库"}
    ):
        dominant_source_terms.discard("cpu")
    if dominant_source_terms & {"cpu", "memory", "disk"}:
        dominant_source_terms.discard("kubernetes")
    if {"redis", "mysql", "kubernetes", "prometheus", "loki"} & dominant_source_terms:
        dominant_source_terms.difference_update({"cpu", "memory", "disk", "service_unavailable"})
    return {
        "preferred_doc_types": preferred_doc_types,
        "preferred_extensions": preferred_extensions,
        "preferred_source_terms": preferred_source_terms,
        "dominant_source_terms": dominant_source_terms,
        "preferred_heading_terms": preferred_heading_terms,
        "required_sources": required_sources,
        "penalize_generic_service": specific_fault_query and not generic_service_query,
        "require_source_diversity": any(
            term in lowered
            for term in {
                "同时",
                "结合",
                "两个",
                "哪两",
                "多来源",
                "多格式",
                "分别",
                "联合",
                "相互印证",
                "交叉验证",
                "转化为",
            }
        ),
        "prefer_service_debug": any(
            term in lowered
            for term in {
                "endpointslice",
                "endpoints",
                "selector",
                "clusterip",
                "service 与 pod",
                "service 后端",
            }
        ),
    }


def retrieval_intent_multiplier(
    chunk: dict[str, Any],
    preferences: dict[str, set[str] | bool],
) -> float:
    """Return a small ranking multiplier without overriding base relevance."""
    source_file = str(chunk.get("source_file") or "")
    metadata = dict(chunk.get("metadata") or {})
    suffix = Path(source_file).suffix.lower()
    doc_type = str(metadata.get("doc_type") or "").strip().lower()
    if not doc_type:
        doc_type = _doc_type_from_suffix(suffix)

    raw_doc_types = preferences.get("preferred_doc_types")
    raw_extensions = preferences.get("preferred_extensions")
    preferred_doc_types = set(raw_doc_types) if isinstance(raw_doc_types, set) else set()
    preferred_extensions = set(raw_extensions) if isinstance(raw_extensions, set) else set()
    raw_source_terms = preferences.get("preferred_source_terms")
    preferred_source_terms = set(raw_source_terms) if isinstance(raw_source_terms, set) else set()
    raw_dominant_terms = preferences.get("dominant_source_terms")
    dominant_source_terms = (
        set(raw_dominant_terms) if isinstance(raw_dominant_terms, set) else set()
    )
    raw_heading_terms = preferences.get("preferred_heading_terms")
    preferred_heading_terms = (
        set(raw_heading_terms) if isinstance(raw_heading_terms, set) else set()
    )
    raw_required_sources = preferences.get("required_sources")
    required_sources = (
        set(raw_required_sources) if isinstance(raw_required_sources, set) else set()
    )
    normalized_source = source_file.lower().replace("-", "_")
    normalized_heading = str(chunk.get("heading_path") or "").lower()
    multiplier = 1.0
    if suffix and suffix in preferred_extensions:
        multiplier *= 1.22
    elif preferred_extensions:
        multiplier *= 0.88
    if doc_type and doc_type in preferred_doc_types:
        multiplier *= 1.12
    elif preferred_doc_types:
        multiplier *= 0.92
    if bool(preferences.get("penalize_generic_service")) and source_file.lower() == (
        "service_unavailable.md"
    ):
        multiplier *= 0.78
    if bool(preferences.get("prefer_service_debug")):
        if "debug_services" in normalized_source or "debug_pods" in normalized_source:
            multiplier *= 1.6
    if source_file.lower() in required_sources:
        multiplier *= 1.65
    if preferred_heading_terms:
        if any(term.lower() in normalized_heading for term in preferred_heading_terms):
            multiplier *= 1.55
        elif any(
            term in normalized_heading
            for term in {"告警名称", "问题描述", "相关告警", "预防措施", "长期优化"}
        ):
            multiplier *= 0.68
    searchable_text = " ".join(
        [
            normalized_source,
            str(chunk.get("heading_path") or "").lower(),
            str(chunk.get("content") or "").lower(),
        ]
    )
    if dominant_source_terms:
        source_matches = sum(term in normalized_source for term in dominant_source_terms)
        content_matches = sum(term in searchable_text for term in dominant_source_terms)
        if source_matches:
            multiplier *= 2.4
        elif content_matches:
            multiplier *= 1.35
        elif source_file.lower() in {
            "cpu_high_usage.md",
            "memory_high_usage.md",
            "disk_high_usage.md",
            "service_unavailable.md",
            "slow_response.md",
        }:
            multiplier *= 0.55
    elif preferred_source_terms:
        if any(term in normalized_source for term in preferred_source_terms):
            multiplier *= 1.45
        elif source_file.lower() in {
            "cpu_high_usage.md",
            "memory_high_usage.md",
            "disk_high_usage.md",
            "service_unavailable.md",
            "slow_response.md",
        }:
            multiplier *= 0.78
    return multiplier


def select_required_sources(
    candidates: list[dict[str, Any]],
    *,
    required_sources: set[str],
    top_k: int,
) -> list[dict[str, Any]]:
    """Reserve one ranked slot for each source explicitly required by the query."""
    if not candidates or not required_sources or top_k <= 0:
        return candidates

    selected_ids: set[tuple[str, str]] = set()
    selected: list[dict[str, Any]] = []
    for source in required_sources:
        match = next(
            (
                chunk
                for chunk in candidates
                if str(chunk.get("source_file") or "").lower() == source.lower()
            ),
            None,
        )
        if match is None:
            continue
        identity = (str(match.get("doc_id") or ""), str(match.get("chunk_id") or ""))
        if identity not in selected_ids:
            selected.append(match)
            selected_ids.add(identity)

    selected.sort(key=lambda item: -float(item.get("rerank_score") or 0.0))
    for chunk in candidates:
        if len(selected) >= top_k:
            break
        identity = (str(chunk.get("doc_id") or ""), str(chunk.get("chunk_id") or ""))
        if identity in selected_ids:
            continue
        selected.append(chunk)
        selected_ids.add(identity)

    selected_identities = {
        (str(item.get("doc_id") or ""), str(item.get("chunk_id") or "")) for item in selected
    }
    return selected + [
        chunk
        for chunk in candidates
        if (str(chunk.get("doc_id") or ""), str(chunk.get("chunk_id") or ""))
        not in selected_identities
    ]


def _required_sources_from_preferences(
    preferences: dict[str, set[str] | bool],
) -> set[str]:
    raw_required_sources = preferences.get("required_sources")
    return set(raw_required_sources) if isinstance(raw_required_sources, set) else set()


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


def _doc_type_from_suffix(suffix: str) -> str:
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".csv", ".xlsx", ".xls", ".tsv"}:
        return "table"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    return suffix.lstrip(".")


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
    ranks.append(max(base_rank, 1))
    return sum(1 / (k + rank) for rank in ranks if rank is not None)


def _coerce_rank(value: Any) -> int | None:
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return None
    return rank if rank > 0 else None


def deduplicate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the best-ranked candidate for each stable chunk identity."""
    seen: set[tuple[str, str]] = set()
    deduped = []
    for chunk in candidates:
        key = (str(chunk.get("doc_id") or ""), str(chunk.get("chunk_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(dict(chunk))
    return deduped


def compute_lexical_score(query_terms: set[str], chunk: dict[str, Any]) -> float:
    """Return a bounded lexical score based on query/document term overlap."""
    metadata = dict(chunk.get("metadata") or {})
    indexed_score = metadata.get("_lexical_score")
    if indexed_score is not None:
        try:
            return min(float(indexed_score) / 5, 1.0)
        except (TypeError, ValueError):
            pass
    if not query_terms:
        return 0.0
    chunk_text = " ".join(
        [
            str(chunk.get("source_file") or ""),
            str(chunk.get("heading_path") or ""),
            str(chunk.get("content") or ""),
        ]
    )
    chunk_terms = extract_retrieval_terms(chunk_text)
    overlap = query_terms & chunk_terms
    if not overlap:
        return 0.0
    return min(1.0, len(overlap) / math.sqrt(len(query_terms) * max(len(chunk_terms), 1)) * 4)


def _trusted_lexical_score(chunk: dict[str, Any], min_lexical_score: float) -> bool:
    lexical_score = _coerce_score(chunk.get("lexical_score"))
    if lexical_score is None:
        return False
    return lexical_score >= min_lexical_score


def extract_retrieval_terms(text: str) -> set[str]:
    """Extract deterministic lexical features from mixed Chinese and ASCII text."""
    lowered = str(text or "").lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_./:-]{1,}", lowered))
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    terms.update("".join(cjk_chars[index : index + 2]) for index in range(len(cjk_chars) - 1))
    terms.update("".join(cjk_chars[index : index + 3]) for index in range(len(cjk_chars) - 2))
    return {term for term in terms if term.strip()}


def normalize_vector_distance(score: Any) -> float:
    """Convert a distance-like score into a bounded relevance score."""
    try:
        distance = float(score)
    except (TypeError, ValueError):
        return 0.5
    if distance < 0:
        return 0.0
    return 1 / (1 + distance)


def lexical_score_to_distance(score: Any) -> float:
    """Convert lexical relevance into a distance-like score for threshold compatibility."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 9999.0
    return 1 / (1 + max(value, 0.0))


def metadata_matches_filter(metadata: dict[str, Any], metadata_filter: dict[str, Any]) -> bool:
    """Apply a small equality/inclusion metadata filter for non-Milvus test stores."""
    for key, expected in normalize_metadata_filter(metadata_filter).items():
        actual = metadata.get(key)
        if isinstance(expected, list):
            if str(actual) not in {str(item) for item in expected}:
                return False
        elif str(actual) != str(expected):
            return False
    return True


def build_milvus_metadata_expr(metadata_filter: dict[str, Any] | None) -> str | None:
    """Build a Milvus JSON metadata expression for exact-match filters."""
    normalized = normalize_metadata_filter(metadata_filter)
    if not normalized:
        return None
    expressions = []
    for key, value in normalized.items():
        metadata_key = key if key.startswith("_") else key
        if isinstance(value, list):
            values = ", ".join(_quote_expr_value(item) for item in value)
            expressions.append(f'metadata["{metadata_key}"] in [{values}]')
        else:
            expressions.append(f'metadata["{metadata_key}"] == {_quote_expr_value(value)}')
    return " and ".join(expressions)


def normalize_metadata_filter(metadata_filter: dict[str, Any] | None) -> dict[str, Any]:
    """Drop empty filter values while preserving exact-match semantics."""
    if not metadata_filter:
        return {}
    normalized: dict[str, Any] = {}
    for key, value in metadata_filter.items():
        safe_key = str(key).strip()
        if not safe_key or value in (None, ""):
            continue
        if not METADATA_FILTER_KEY_PATTERN.fullmatch(safe_key):
            logger.warning(f"忽略非法 metadata filter key: {safe_key}")
            continue
        if isinstance(value, list):
            items = [item for item in value if item not in (None, "")]
            if items:
                normalized[safe_key] = items
        else:
            normalized[safe_key] = value
    return normalized


def build_retrieval_mode(
    hybrid_search_enabled: bool,
    rerank_enabled: bool,
    fusion_strategy: str | None = None,
) -> str:
    """Return a stable retrieval-mode label for observability and eval reports."""
    strategy = normalize_fusion_strategy(fusion_strategy)
    if hybrid_search_enabled and rerank_enabled:
        return (
            "hybrid_vector_lexical_rrf_rerank"
            if strategy == "rrf"
            else "hybrid_vector_lexical_rerank"
        )
    if hybrid_search_enabled:
        return "hybrid_vector_lexical_rrf" if strategy == "rrf" else "hybrid_vector_lexical"
    if rerank_enabled:
        return "vector_rrf_rerank" if strategy == "rrf" else "vector_rerank"
    return "vector"


def build_degraded_retrieval_mode(
    rerank_enabled: bool,
    fusion_strategy: str | None = None,
) -> str:
    """Return the retrieval mode used when vector search falls back to lexical only."""
    strategy = normalize_fusion_strategy(fusion_strategy)
    if not rerank_enabled:
        return "lexical_degraded"
    return "lexical_degraded_rrf_rerank" if strategy == "rrf" else "lexical_degraded_rerank"


def build_public_vector_error_message(error_detail: str) -> str:
    """Return a frontend-safe vector retrieval error summary."""
    if not error_detail:
        return ""
    return "向量检索暂不可用，已降级使用本地词法索引。"


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
        try:
            return cast(
                list[tuple[Document, Any]],
                vector_store.similarity_search_with_score(query, k=top_k, expr=expr),
            )
        except TypeError:
            return cast(
                list[tuple[Document, Any]],
                vector_store.similarity_search_with_score(query, k=top_k),
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
        try:
            return cast(list[Document], vector_store.similarity_search(query, k=top_k, expr=expr))
        except TypeError:
            return cast(list[Document], vector_store.similarity_search(query, k=top_k))
    return cast(list[Document], vector_store.similarity_search(query, k=top_k))


def _candidate_count(top_k: int) -> int:
    multiplier = max(int(config.rag_hybrid_candidate_multiplier or 1), 1)
    return max(top_k, top_k * multiplier)


def _document_identity(document: Document) -> tuple[str, str]:
    metadata = dict(document.metadata or {})
    source = str(metadata.get("_doc_id") or metadata.get("_source") or metadata.get("source") or "")
    chunk_id = str(metadata.get("_chunk_id") or metadata.get("chunk_id") or "")
    if source or chunk_id:
        return source, chunk_id
    digest = hashlib.sha256(str(document.page_content or "").encode()).hexdigest()
    return "", digest


def _coerce_score(score: Any) -> float | None:
    try:
        return float(score)
    except (TypeError, ValueError):
        return None


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)


def _stable_chunk_id(source_file: str, heading_path: str, content: str) -> str:
    digest = hashlib.sha256(f"{source_file}\n{heading_path}\n{content}".encode()).hexdigest()
    return f"chunk-{digest[:12]}"


def _quote_expr_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
