"""Frontend/API read models for structured RAG retrieval payloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def compact_retrieval_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only frontend-safe retrieval fields."""
    return {
        "status": payload.get("status", "unknown"),
        "query": payload.get("query", ""),
        "source": payload.get("source", "rag"),
        "top_k": payload.get("top_k"),
        "candidate_k": payload.get("candidate_k"),
        "vector_candidate_count": payload.get("vector_candidate_count"),
        "lexical_candidate_count": payload.get("lexical_candidate_count"),
        "max_l2_distance": payload.get("max_l2_distance"),
        "retrieval_mode": payload.get("retrieval_mode", ""),
        "metadata_filter": payload.get("metadata_filter", {}),
        "metadata_filter_expr": payload.get("metadata_filter_expr"),
        "summary": payload.get("summary", ""),
        "answer_policy": payload.get("answer_policy", ""),
        "no_answer_rejected": bool(payload.get("no_answer_rejected")),
        "retrieval_results": build_citations(payload),
        "rejected_results": [
            compact_retrieval_chunk(item)
            for item in payload.get("rejected_results", []) or []
            if isinstance(item, dict)
        ],
        "error_message": payload.get("error_message"),
    }


def build_citations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract stable citation fields from trusted retrieval results."""
    return [
        compact_retrieval_chunk(item)
        for item in payload.get("retrieval_results", []) or []
        if isinstance(item, dict)
    ]


def compact_retrieval_chunk(item: dict[str, Any]) -> dict[str, Any]:
    """Return one citation/search result for API and frontend display."""
    return {
        "rank": item.get("rank"),
        "doc_id": item.get("doc_id", ""),
        "source_file": item.get("source_file", "未知来源"),
        "source_path": public_source_path(item.get("source_path") or item.get("source_file")),
        "heading_path": item.get("heading_path", ""),
        "chunk_id": item.get("chunk_id", ""),
        "score": item.get("score"),
        "lexical_score": item.get("lexical_score"),
        "vector_score": item.get("vector_score"),
        "rerank_score": item.get("rerank_score"),
        "content_preview": item.get("content_preview", ""),
        "retrieval_reason": item.get("retrieval_reason", ""),
    }


def build_runbook_summary(payload: dict[str, Any]) -> str:
    """Create a compact summary for runbook retrieval results."""
    if payload.get("status") == "success":
        results = payload.get("retrieval_results") or []
        sources = sorted(
            {str(item.get("source_file")) for item in results if item.get("source_file")}
        )
        source_text = "、".join(sources[:3]) if sources else "未知来源"
        return f"Runbook 检索命中 {len(results)} 条可信片段，来源：{source_text}"
    if payload.get("status") == "no_answer":
        return "未找到可信 Runbook 来源"
    return str(payload.get("summary") or "Runbook 检索失败")


def public_source_path(value: Any) -> str:
    """Return a frontend-safe source identifier without leaking server directories."""
    text = str(value or "").strip()
    if not text:
        return ""
    return Path(text).name


def format_score(score: Any) -> str:
    """Format retrieval score without breaking on backend-specific values."""
    if score is None:
        return "unknown"
    try:
        return f"{float(score):.4f}"
    except (TypeError, ValueError):
        return str(score)
