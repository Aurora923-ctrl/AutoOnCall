"""Retrieval candidate conversion, identity, scoring, and deduplication."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from langchain_core.documents import Document
from loguru import logger

from app.config import config
from app.services.lexical_index_service import lexical_index_service


def document_to_retrieval_chunk(
    document: Document,
    *,
    score: float | None,
    rank: int,
) -> dict[str, Any]:
    """Convert a LangChain Document into a stable retrieval chunk payload."""
    metadata = dict(document.metadata or {})
    raw_source = metadata.get("_source") or metadata.get("source")
    raw_source_file = metadata.get("_file_name") or metadata.get("source_file") or raw_source
    raw_source_id = (
        metadata.get("_source_id")
        or metadata.get("_doc_id")
        or metadata.get("doc_id")
        or raw_source
        or raw_source_file
    )
    raw_content = document.page_content
    source = str(raw_source or "")
    source_file = str(raw_source_file or "未知来源")
    source_id = str(raw_source_id or "")
    content = str(raw_content or "")
    heading_path = build_heading_path(metadata)
    chunk_id = str(
        metadata.get("_chunk_id")
        or metadata.get("chunk_id")
        or metadata.get("id")
        or _stable_chunk_id(source_file, heading_path, content)
    )
    doc_id = source_id

    return {
        "rank": rank,
        "doc_id": doc_id,
        "source_file": source_file,
        "source_id": source_id,
        "source_path": source,
        "heading_path": heading_path,
        "chunk_id": chunk_id,
        "score": score,
        "content_preview": content[: config.rag_content_preview_chars],
        "content": content,
        "metadata": metadata,
        # A generated fallback citation is useful for diagnostics, but it is
        # not provenance.  The trust gate must not treat it as auditable.
        "metadata_identity_valid": bool(
            isinstance(raw_content, str)
            and isinstance(raw_source_file, str)
            and isinstance(raw_source_id, str)
            and content
            and any(
                metadata.get(key)
                for key in (
                    "_source",
                    "source",
                    "_source_id",
                    "_doc_id",
                    "_file_name",
                    "source_file",
                )
            )
            and any(metadata.get(key) for key in ("_chunk_id", "chunk_id", "id"))
        ),
        "metadata_types_valid": bool(
            isinstance(raw_content, str)
            and isinstance(raw_source_file, str)
            and isinstance(raw_source_id, str)
        ),
    }


def disambiguate_citation_sources(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Make citation source labels unique only when basename/chunk pairs collide."""
    identities_by_citation: dict[tuple[str, str], set[str]] = {}
    for chunk in candidates:
        citation = (
            str(chunk.get("source_file") or ""),
            str(chunk.get("chunk_id") or ""),
        )
        identities_by_citation.setdefault(citation, set()).add(
            str(chunk.get("source_id") or chunk.get("doc_id") or "")
        )

    disambiguated: list[dict[str, Any]] = []
    for chunk in candidates:
        citation = (
            str(chunk.get("source_file") or ""),
            str(chunk.get("chunk_id") or ""),
        )
        if len(identities_by_citation.get(citation, set())) <= 1:
            disambiguated.append(chunk)
            continue
        updated = dict(chunk)
        updated["source_file"] = _public_source_identity(
            chunk.get("source_id") or chunk.get("doc_id") or chunk.get("source_path")
        )
        disambiguated.append(updated)
    return disambiguated


def is_stale_retrieval_source(
    chunk: dict[str, Any],
    *,
    lexical_index: Any | None = None,
) -> bool:
    """Return True when an indexed source was superseded but failed to re-index."""
    source_path = str(chunk.get("source_path") or "")
    if not source_path:
        return False
    stale_registry = lexical_index or lexical_index_service
    try:
        return stale_registry.is_source_stale(source_path)
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
        # Backend provenance is authoritative.  Never inherit a user/index
        # supplied retrieval label, otherwise a lexical score can masquerade
        # as a vector hit (or vice versa) at the trust gate.
        metadata["_retrieval_source"] = "vector"
        if key in merged:
            existing_document, existing_score, existing_position = merged[key]
            if str(document.page_content or "") != str(existing_document.page_content or ""):
                logger.warning(
                    "跳过 identity 相同但正文不同的 vector 候选: source={}, chunk_id={}",
                    key[0],
                    key[1],
                )
                continue
            existing_value = _coerce_score(existing_score)
            candidate_value = _coerce_score(score)
            # Milvus L2 distance is lower-is-better.  Preserve the closest
            # duplicate, not the largest distance.
            if existing_value is not None and (
                candidate_value is None or candidate_value >= existing_value
            ):
                continue
            position = existing_position
            metadata["_vector_rank"] = existing_position
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
            if str(document.page_content or "") != str(existing_document.page_content or ""):
                logger.warning(
                    "跳过 identity 相同但正文不同的 lexical 候选: source={}, chunk_id={}",
                    key[0],
                    key[1],
                )
                continue
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


def deduplicate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the best-ranked candidate for each stable chunk identity."""
    positions: dict[tuple[str, str], int] = {}
    deduped: list[dict[str, Any]] = []
    for chunk in candidates:
        key = (
            str(chunk.get("source_file") or chunk.get("doc_id") or ""),
            str(chunk.get("chunk_id") or ""),
        )
        existing_position = positions.get(key)
        if existing_position is None:
            positions[key] = len(deduped)
            deduped.append(dict(chunk))
            continue
        current = deduped[existing_position]
        if normalize_chunk_content(chunk) != normalize_chunk_content(current):
            conflicted = dict(current)
            conflicted["identity_conflict"] = True
            conflicted["conflicting_chunk_hashes"] = sorted(
                {
                    chunk_content_hash(chunk),
                    chunk_content_hash(current),
                }
            )
            deduped[existing_position] = conflicted
            continue
        current = deduped[existing_position]
        merged = dict(current)
        merged_metadata = _merge_candidate_metadata(
            current.get("metadata"),
            chunk.get("metadata"),
        )
        merged["metadata"] = merged_metadata
        for signal in (
            "score",
            "lexical_score",
            "vector_score",
            "rerank_score",
            "rrf_score",
        ):
            if signal not in chunk:
                continue
            if _score_is_better(chunk.get(signal), current.get(signal), lower_is_better=signal == "score"):
                merged[signal] = chunk[signal]
        if _candidate_is_better(chunk, current):
            merged.update(dict(chunk))
            merged["metadata"] = merged_metadata
        deduped[existing_position] = merged
    return deduped


def _merge_candidate_metadata(
    current_metadata: Any,
    candidate_metadata: Any,
) -> dict[str, Any]:
    """Merge duplicate metadata without allowing a weaker hit to erase signals."""
    current = dict(current_metadata) if isinstance(current_metadata, dict) else {}
    candidate = dict(candidate_metadata) if isinstance(candidate_metadata, dict) else {}
    merged = dict(current)

    for key, value in candidate.items():
        if key not in merged or merged[key] in (None, ""):
            merged[key] = value

    for signal in ("_vector_score", "_lexical_score"):
        candidate_value = _coerce_score(candidate.get(signal))
        current_value = _coerce_score(current.get(signal))
        if candidate_value is not None and (
            current_value is None
            or (
                signal == "_vector_score"
                and candidate_value < current_value
            )
            or (
                signal == "_lexical_score"
                and candidate_value > current_value
            )
        ):
            merged[signal] = candidate_value

    for rank_key in ("_vector_rank", "_lexical_rank"):
        candidate_rank = _positive_rank(candidate.get(rank_key))
        current_rank = _positive_rank(current.get(rank_key))
        if candidate_rank is not None and (
            current_rank is None or candidate_rank < current_rank
        ):
            merged[rank_key] = candidate_rank

    sources = {
        str(value).strip().lower()
        for value in (current.get("_retrieval_source"), candidate.get("_retrieval_source"))
        if str(value).strip().lower() in {"vector", "lexical", "hybrid"}
    }
    if sources:
        merged["_retrieval_source"] = (
            "hybrid"
            if len(sources) > 1 or "hybrid" in sources
            else next(iter(sources))
        )
    return merged


def _positive_rank(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return None
    return rank if rank > 0 else None


def _candidate_is_better(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    candidate_score = _coerce_score(candidate.get("score"))
    current_score = _coerce_score(current.get("score"))
    if candidate_score is not None and current_score is not None:
        return candidate_score < current_score
    if candidate_score is not None:
        return True
    if current_score is not None:
        return False
    candidate_lexical = _coerce_score(candidate.get("metadata", {}).get("_lexical_score"))
    current_lexical = _coerce_score(current.get("metadata", {}).get("_lexical_score"))
    return (candidate_lexical or 0.0) > (current_lexical or 0.0)


def _score_is_better(candidate: Any, current: Any, *, lower_is_better: bool) -> bool:
    candidate_value = _coerce_score(candidate)
    current_value = _coerce_score(current)
    if candidate_value is None:
        return False
    if current_value is None:
        return True
    return candidate_value < current_value if lower_is_better else candidate_value > current_value


def citation_source_basename(value: Any) -> str:
    """Normalize a public or disambiguated source label for coverage checks."""
    return str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1].lower()


def normalize_chunk_content(chunk: dict[str, Any]) -> str:
    """Normalize chunk text before checking stable identity conflicts."""
    return re.sub(
        r"\s+",
        " ",
        str(chunk.get("content") or chunk.get("content_preview") or "").strip(),
    )


def chunk_content_hash(chunk: dict[str, Any]) -> str:
    """Return an observed or computed content hash for conflict diagnostics."""
    metadata = chunk.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    observed = str(metadata.get("_chunk_hash") or "").strip()
    if observed:
        return observed
    return hashlib.sha256(normalize_chunk_content(chunk).encode("utf-8")).hexdigest()


def compute_lexical_score(query_terms: set[str], chunk: dict[str, Any]) -> float:
    """Return a bounded lexical score based on query/document term overlap."""
    metadata = dict(chunk.get("metadata") or {})
    indexed_score = metadata.get("_lexical_score")
    if indexed_score is not None:
        try:
            return max(0.0, min(float(indexed_score) / 5, 1.0))
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
        return 0.0
    if not math.isfinite(distance) or distance < 0:
        return 0.0
    return 1 / (1 + distance)


def lexical_score_to_distance(score: Any) -> float:
    """Convert lexical relevance into a distance-like score for threshold compatibility."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 9999.0
    if not math.isfinite(value):
        return 9999.0
    return 1 / (1 + max(value, 0.0))


def _document_identity(document: Document) -> tuple[str, str]:
    metadata = dict(document.metadata or {})
    source = str(
        metadata.get("_source_id")
        or metadata.get("_doc_id")
        or metadata.get("_source")
        or metadata.get("source")
        or ""
    )
    chunk_id = str(metadata.get("_chunk_id") or metadata.get("chunk_id") or "")
    if source or chunk_id:
        return source, chunk_id
    digest = hashlib.sha256(str(document.page_content or "").encode()).hexdigest()
    return "", digest


def _coerce_score(score: Any) -> float | None:
    if isinstance(score, bool):
        return None
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _format_retrieval_score(score: Any) -> str:
    value = _coerce_score(score)
    return "未知" if value is None else f"{value:.4f}"


def _stable_chunk_id(source_file: str, heading_path: str, content: str) -> str:
    digest = hashlib.sha256(f"{source_file}\n{heading_path}\n{content}".encode()).hexdigest()
    return f"chunk-{digest[:12]}"


def _public_source_identity(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return "未知来源"
    lowered = text.lower()
    for marker in ("docs/knowledge-base/", "uploads/"):
        position = lowered.rfind(marker)
        if position >= 0:
            return text[position:]
    if re.match(r"^[A-Za-z]:/", text) or text.startswith("/"):
        basename = text.rsplit("/", 1)[-1] or "document"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return f"{basename}@{digest}"
    return text


CITATION_INSTRUCTION = (
    "\u5f15\u7528\u8981\u6c42: \u4ec5\u57fa\u4e8e\u4e0b\u5217\u53ef\u4fe1\u77e5\u8bc6\u56de\u7b54\uff1b"
    "\u56de\u7b54\u672b\u5c3e\u5217\u51fa\u5f15\u7528\u6765\u6e90\uff0c"
    "\u683c\u5f0f\u4e3a source_file + chunk_id\u3002"
)


def format_retrieval_results(results: list[dict[str, Any]]) -> str:
    """Format structured retrieval chunks as LLM-readable context."""
    if not results:
        return "\u672a\u627e\u5230\u53ef\u4fe1\u77e5\u8bc6\u6765\u6e90\u3002"

    parts: list[str] = [CITATION_INSTRUCTION]
    for item in results:
        score_text = _format_retrieval_score(item.get("score"))
        heading = str(item.get("heading_path") or "").strip()
        source = str(item.get("source_file") or "鏈煡鏉ユ簮")
        chunk_id = str(item.get("chunk_id") or "")
        content = str(item.get("content") or item.get("content_preview") or "").strip()
        metadata = dict(item.get("metadata") or {})

        lines = [
            f"\u3010\u53ef\u4fe1\u77e5\u8bc6 {item.get('rank', len(parts) + 1)}\u3011",
            f"source_file: {source}",
            f"chunk_id: {chunk_id}",
            f"score: {score_text}",
        ]
        if metadata.get("page_number") is not None:
            lines.append(f"page_number: {metadata.get('page_number')}")
        if metadata.get("sheet_name"):
            lines.append(f"sheet_name: {metadata.get('sheet_name')}")
        if metadata.get("row_number") is not None:
            lines.append(f"row_number: {metadata.get('row_number')}")
        if metadata.get("primary_key"):
            lines.append(f"primary_key: {metadata.get('primary_key')}")
        if heading:
            lines.append(f"鏍囬璺緞: {heading}")
        lines.extend(["鍐呭:", content])
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def documents_to_context(docs: list[Document]) -> str:
    """Format existing Document lists through the structured retrieval representation."""
    chunks = [
        document_to_retrieval_chunk(document, score=None, rank=rank)
        for rank, document in enumerate(docs, 1)
    ]
    return format_retrieval_results(chunks)
