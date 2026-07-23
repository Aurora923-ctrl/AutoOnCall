"""Trust gates, source coverage, and final candidate selection."""

from __future__ import annotations

import re
from typing import Any

from app.config import config
from app.services.policies.retrieval_policy import (
    LEXICAL_RETRIEVAL_SOURCES,
    VECTOR_RETRIEVAL_SOURCES,
    is_trusted_l2_distance,
)
from app.services.rag_retrieval.candidates import (
    _coerce_score,
    citation_source_basename,
    extract_retrieval_terms,
)
from app.services.rag_retrieval.intent import query_has_oncall_scope


def select_required_sources(
    candidates: list[dict[str, Any]],
    *,
    required_sources: set[str],
    top_k: int,
) -> list[dict[str, Any]]:
    """Reserve one ranked slot for each source explicitly required by the query."""
    if not candidates or not required_sources or top_k <= 0:
        return candidates
    if len(required_sources) > top_k:
        return candidates[:top_k]

    selected_ids: set[tuple[str, str]] = set()
    selected: list[dict[str, Any]] = []
    for source in sorted(required_sources):
        match = next(
            (
                chunk
                for chunk in candidates
                if citation_source_basename(chunk.get("source_file")) == source.lower()
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
    required_candidates = [
        chunk
        for chunk in candidates
        if citation_source_basename(chunk.get("source_file")) in required_sources
    ]
    if len(required_sources) == 1:
        # A single explicit source requirement means the query is asking for
        # multiple semantic facets from one runbook. Fill remaining slots from
        # that source before admitting adjacent-domain chunks.
        required_candidates.sort(
            key=lambda item: -float(item.get("rerank_score") or 0.0)
        )
    for chunk in required_candidates:
        if len(selected) >= top_k:
            break
        identity = (str(chunk.get("doc_id") or ""), str(chunk.get("chunk_id") or ""))
        if identity in selected_ids:
            continue
        selected.append(chunk)
        selected_ids.add(identity)

    if len(required_sources) == 1:
        # Avoid spending the final slot on document metadata when the query
        # asks for diagnosis or action from one explicitly required runbook.
        semantic_candidates = [
            chunk
            for chunk in selected
            if not _is_metadata_only_chunk(chunk)
        ]
        if semantic_candidates:
            selected = semantic_candidates

    selected_identities = {
        (str(item.get("doc_id") or ""), str(item.get("chunk_id") or "")) for item in selected
    }
    return selected + [
        chunk
        for chunk in candidates
        if (str(chunk.get("doc_id") or ""), str(chunk.get("chunk_id") or ""))
        not in selected_identities
    ]


def select_heading_coverage(
    candidates: list[dict[str, Any]],
    *,
    query: str,
    top_k: int,
) -> list[dict[str, Any]]:
    """Reserve evidence, command, and safety-boundary chunks requested by the query."""
    if not candidates or top_k <= 1:
        return candidates
    heading_groups = retrieval_heading_groups(query)
    if not heading_groups:
        return candidates

    selected: list[dict[str, Any]] = []
    selected_ids: set[tuple[str, str]] = set()
    query_entities = _query_service_entities(query)

    boundary_requested = any(
        term in str(query or "").lower()
        for term in {
            "边界",
            "审批",
            "重启",
            "扩容",
            "回滚",
            "清理",
            "删除",
            "截断",
            "写文件失败",
            "处置",
            "限流",
        }
    )
    for heading_terms in heading_groups:
        match = next(
            (
                chunk
                for chunk in candidates
                if (
                    not query_entities
                    or any(
                        entity in _candidate_searchable_text(chunk)
                        for entity in query_entities
                    )
                )
                if any(
                    term in str(chunk.get("heading_path") or "").lower()
                    for term in heading_terms
                )
            ),
            None,
        )
        if match is None:
            continue
        if query_entities and not any(
            entity in _candidate_searchable_text(match) for entity in query_entities
        ):
            continue
        identity = (str(match.get("doc_id") or ""), str(match.get("chunk_id") or ""))
        if identity in selected_ids:
            continue
        selected.append(match)
        selected_ids.add(identity)
        if len(selected) >= top_k:
            break

    if query_entities and len(selected) < top_k:
        entity_matches = [
            chunk
            for chunk in candidates
            if any(entity in _candidate_searchable_text(chunk) for entity in query_entities)
        ]
        for chunk in entity_matches:
            if len(selected) >= top_k:
                break
            identity = (str(chunk.get("doc_id") or ""), str(chunk.get("chunk_id") or ""))
            if identity in selected_ids:
                continue
            selected.append(chunk)
            selected_ids.add(identity)

    primary_source = citation_source_basename(candidates[0].get("source_file"))
    same_source = [
        chunk
        for chunk in candidates
        if citation_source_basename(chunk.get("source_file")) == primary_source
    ]
    remaining_candidates = same_source + [
        chunk
        for chunk in candidates
        if citation_source_basename(chunk.get("source_file")) != primary_source
    ]
    for chunk in remaining_candidates:
        if len(selected) >= top_k:
            break
        identity = (str(chunk.get("doc_id") or ""), str(chunk.get("chunk_id") or ""))
        if identity in selected_ids:
            continue
        selected.append(chunk)
        selected_ids.add(identity)
    if boundary_requested and len(selected) >= top_k:
        boundary_candidate = next(
            (
                chunk
                for chunk in candidates
                if any(
                    term in str(chunk.get("heading_path") or "").lower()
                    for term in {
                        "处置计划与审批",
                        "升级与审批",
                        "change and approval boundary",
                        "回滚条件",
                        "rollback conditions",
                    }
                )
            ),
            None,
        )
        if boundary_candidate is not None:
            boundary_identity = (
                str(boundary_candidate.get("doc_id") or ""),
                str(boundary_candidate.get("chunk_id") or ""),
            )
            if boundary_identity not in selected_ids:
                selected[-1] = boundary_candidate
                selected_ids = {
                    (str(item.get("doc_id") or ""), str(item.get("chunk_id") or ""))
                    for item in selected
                }
    return selected + [
        chunk
        for chunk in candidates
        if (str(chunk.get("doc_id") or ""), str(chunk.get("chunk_id") or ""))
        not in selected_ids
    ]


def _is_metadata_only_chunk(chunk: dict[str, Any]) -> bool:
    heading = str(chunk.get("heading_path") or "").lower()
    content = str(chunk.get("content") or chunk.get("content_preview") or "").lower()
    metadata_markers = ("文档元数据", "scope and ownership", "owner", "last reviewed")
    semantic_markers = (
        "证据",
        "metric",
        "pool_waiting",
        "active_connections",
        "decision",
        "rollback",
        "审批",
        "回滚",
    )
    return (
        any(marker in heading for marker in metadata_markers)
        and not any(marker in content for marker in semantic_markers)
    )


def _query_service_entities(query: str) -> set[str]:
    """Extract explicit service names used to keep table history rows on-topic."""
    return {
        match.group(0).lower()
        for match in re.finditer(
            r"\b[a-z][a-z0-9-]*-service\b",
            str(query or ""),
            flags=re.IGNORECASE,
        )
    }


def _candidate_searchable_text(chunk: dict[str, Any]) -> str:
    metadata = chunk.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return " ".join(
        (
            str(chunk.get("source_file") or ""),
            str(chunk.get("heading_path") or ""),
            str(chunk.get("content") or chunk.get("content_preview") or ""),
            str(metadata.get("primary_key") or ""),
        )
    ).lower()


def retrieval_heading_groups(query: str) -> list[tuple[str, ...]]:
    """Return ordered heading groups needed to answer explicit investigation subgoals."""
    lowered = str(query or "").lower()
    groups: list[tuple[str, ...]] = []
    if any(
        term in lowered
        for term in {
            "取证",
            "排查",
            "如何判断",
            "判断",
            "原因",
            "怎样区分",
            "怎么区分",
            "如何验证",
            "如何收集",
        }
    ):
        groups.append(
            (
                "首轮证据",
                "证据与指标查询",
                "evidence and metric queries",
                "排查步骤",
                "只读取证命令",
                "常用命令",
                "相关工具命令",
                "diagnosing",
                "evidence",
                "read-only diagnosis",
                "monitoring ingestion errors",
            )
        )
    if any(
        term in lowered
        for term in {
            "区分",
            "判断",
            "主故障域",
            "根因",
            "慢查询",
            "连接池",
            "pool_waiting",
            "active_connections",
            "503",
            "5xx",
        }
    ):
        groups.append(
            (
                "假设排除与决策树",
                "原因判别",
                "decision tree",
                "read-only diagnosis",
                "排查步骤",
            )
        )
    if any(
        term in lowered
        for term in {
            "进程",
            "线程",
            "inode",
            "大目录",
            "oom",
            "oomkilled",
        }
    ):
        groups.append(
            (
                "首轮证据",
                "只读取证命令",
                "常用命令",
                "相关工具命令",
                "排查步骤",
            )
        )
    if any(
        term in lowered
        for term in {
            "边界",
            "重启",
            "扩容",
            "回滚",
            "清理",
            "删除",
            "截断",
            "写文件失败",
            "处置",
            "审批",
            "限流",
            "设计症状告警",
            "503",
            "5xx",
            "pool_waiting",
            "active_connections",
            "慢查询",
            "索引",
            "连接池",
        }
    ):
        groups.append(
            (
                "处置计划与审批",
                "升级与审批",
                "change and approval boundary",
                "回滚条件",
                "rollback conditions",
                "审批",
                "安全",
                "快速决策摘要",
            )
        )
    if any(
        term in lowered
        for term in {"历史", "当前", "实时", "incident-window", "复盘", "工单", "部署历史"}
    ):
        groups.append(("快速决策摘要", "incident-window", "历史", "部署历史", "工单"))
    if any(
        term in lowered
        for term in {
            "cpu",
            "oom",
            "oomkilled",
            "disk",
            "inode",
            "pool_waiting",
            "active_connections",
            "endpointslice",
            "discarded",
        }
    ):
        groups.append(
            (
                "quick decision summary",
                "change and approval boundary",
                "处置计划与审批",
                "假设排除与决策树",
                "decision tree",
                "快速决策摘要",
            )
        )
    return groups


def select_diverse_sources(
    candidates: list[dict[str, Any]],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    """Prefer distinct sources before filling remaining ranked slots."""
    if not candidates or top_k <= 1:
        return candidates

    selected: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for chunk in candidates:
        source = str(chunk.get("source_file") or "").strip().lower()
        if source and source not in seen_sources:
            selected.append(chunk)
            seen_sources.add(source)
            if len(selected) >= top_k:
                break

    selected_ids = {id(item) for item in selected}
    return selected + [item for item in candidates if id(item) not in selected_ids]


def enforce_source_coverage(
    candidates: list[dict[str, Any]],
    *,
    required_sources: set[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Fail closed when a required multi-source query loses a source."""
    if not required_sources:
        return candidates, set()
    present = {
        citation_source_basename(item.get("source_file"))
        for item in candidates
        if str(item.get("source_file") or "").strip()
    }
    required = {str(source).strip().lower() for source in required_sources if str(source).strip()}
    missing = required - present
    return (candidates if not missing else [], missing)


def is_trusted_retrieval_chunk(
    chunk: dict[str, Any],
    *,
    max_distance: float,
    min_lexical_score: float,
) -> bool:
    """Return True when either vector distance or lexical score crosses its trust gate."""
    if (
        chunk.get("identity_conflict")
        or not chunk.get("metadata_identity_valid", True)
        or not str(chunk.get("content") or chunk.get("content_preview") or "").strip()
    ):
        return False
    metadata = dict(chunk.get("metadata") or {})
    retrieval_source = str(metadata.get("_retrieval_source") or "")
    has_vector_signal = (
        retrieval_source in VECTOR_RETRIEVAL_SOURCES
        or metadata.get("_vector_score") is not None
    )
    if has_vector_signal and is_trusted_l2_distance(chunk.get("score"), max_distance):
        return True
    if retrieval_source in LEXICAL_RETRIEVAL_SOURCES:
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
    if chunk.get("identity_conflict"):
        return "same source_file + chunk_id maps to different content versions"
    metadata = dict(chunk.get("metadata") or {})
    retrieval_source = str(metadata.get("_retrieval_source") or "")
    score = chunk.get("score")
    if retrieval_source in {"vector", "hybrid"} or metadata.get("_vector_score") is not None:
        if score is None:
            vector_reason = "\u68c0\u7d22\u540e\u7aef\u672a\u8fd4\u56de\u8ddd\u79bb\u5206\u6570"
        else:
            try:
                score_value = float(score)
                relation = (
                    "\u5c0f\u4e8e\u7b49\u4e8e"
                    if score_value <= max_distance
                    else "\u5927\u4e8e"
                )
                vector_reason = (
                    f"L2 distance {score_value:.4f} {relation} "
                    f"\u9608\u503c {max_distance:.4f}"
                )
            except (TypeError, ValueError):
                vector_reason = f"\u8ddd\u79bb\u5206\u6570\u4e0d\u53ef\u89e3\u6790: {score}"
        if trusted and is_trusted_l2_distance(score, max_distance):
            return vector_reason
        if retrieval_source != "hybrid":
            return vector_reason

    lexical_score = _coerce_score(chunk.get("lexical_score"))
    if lexical_score is None:
        return (
            "\u8bcd\u6cd5\u53ec\u56de\u7f3a\u5c11\u53ef\u4fe1\u5206\u6570"
            if not trusted
            else "\u8bcd\u6cd5\u53ec\u56de\u901a\u8fc7\u53ef\u4fe1\u9608\u503c"
        )
    relation = (
        "\u5927\u4e8e\u7b49\u4e8e"
        if lexical_score >= min_lexical_score
        else "\u5c0f\u4e8e"
    )
    return (
        f"lexical score {lexical_score:.4f} {relation} "
        f"\u9608\u503c {min_lexical_score:.4f}"
    )


def query_is_out_of_scope(query: str, candidates: list[dict[str, Any]]) -> bool:
    """Reject semantically unrelated vector hits instead of answering from noise."""
    lowered = str(query or "").lower()
    if not candidates or not lowered:
        return False
    if not query_has_oncall_scope(lowered):
        return True
    query_terms = extract_retrieval_terms(query)
    if not query_terms:
        return False
    candidate_terms = set()
    for candidate in candidates:
        candidate_terms.update(
            extract_retrieval_terms(
                " ".join(
                    [
                        str(candidate.get("source_file") or ""),
                        str(candidate.get("heading_path") or ""),
                        str(candidate.get("content") or ""),
                    ]
                )
            )
        )
    if query_terms & candidate_terms:
        return False
    # `score` is an L2 distance, so lower is better.  Comparing its inverted
    # relevance score with a distance threshold makes the OOD gate ineffective
    # for normal thresholds (the inverted score is always <= 1).
    vector_distances = []
    for candidate in candidates:
        value = _coerce_score(candidate.get("score"))
        if value is not None and value >= 0:
            vector_distances.append(value)
    best_vector_distance = min(vector_distances, default=float("inf"))
    best_lexical_score = max(
        (_coerce_score(candidate.get("lexical_score")) or 0.0 for candidate in candidates),
        default=0.0,
    )
    return (
        best_vector_distance > config.rag_max_l2_distance * 0.70
        and best_lexical_score <= 0.0
    )


def _trusted_lexical_score(chunk: dict[str, Any], min_lexical_score: float) -> bool:
    lexical_score = _coerce_score(chunk.get("lexical_score"))
    if lexical_score is None:
        return False
    return lexical_score >= min_lexical_score
