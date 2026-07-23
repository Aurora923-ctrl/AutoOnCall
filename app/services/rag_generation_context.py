"""Generation-time evidence selection, deduplication, and context budgeting."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.services.context_budget import DEFAULT_CONTEXT_BUDGETER, ContextBudgeter
from app.services.rag_retrieval.candidates import extract_retrieval_terms

GENERATION_EVIDENCE_TARGET_CHARS = 900


def format_frozen_generation_context(evidence: Any) -> str:
    """Render only the exact content and citation labels assigned during freezing."""
    return "\n\n".join(
        _format_evidence_block(
            item.get("citation_index") or index,
            source_file=str(item.get("source_file") or "未知来源").strip(),
            chunk_id=str(item.get("chunk_id") or "unknown").strip(),
            content=str(item.get("content") or item.get("content_preview") or ""),
        )
        for index, item in enumerate(evidence.items, 1)
    )


def build_generation_context(
    retrieval_payload: dict[str, Any],
    *,
    budgeter: ContextBudgeter | None = None,
    limit: int | None = None,
) -> str:
    """Build a de-duplicated evidence block without changing retrieval results."""
    frozen_evidence = retrieval_payload.get("_frozen_generation_evidence")
    if frozen_evidence is not None:
        return format_frozen_generation_context(frozen_evidence)
    evidence = build_generation_evidence(
        retrieval_payload,
        budgeter=budgeter,
        limit=limit,
    )
    if not evidence:
        return ""
    return "\n\n".join(
        _format_evidence_block(
            index,
            source_file=str(item.get("source_file") or "未知来源").strip(),
            chunk_id=str(item.get("chunk_id") or "unknown").strip(),
            content=str(item.get("content") or item.get("content_preview") or "").strip(),
        )
        for index, item in enumerate(evidence, 1)
    )

def build_generation_evidence(
    retrieval_payload: dict[str, Any],
    *,
    budgeter: ContextBudgeter | None = None,
    limit: int | None = None,
    select_excerpts: bool = True,
) -> list[dict[str, Any]]:
    """Freeze the de-duplicated, budgeted evidence set used for generation."""
    results = retrieval_payload.get("retrieval_results") or []
    if not isinstance(results, list) or not results:
        return []

    raw_allowlist = retrieval_payload.get("generation_allowlist")
    allowlist = {
        (
            str(item.get("source_file") or "").strip(),
            str(item.get("chunk_id") or "").strip(),
        )
        for item in raw_allowlist or []
        if isinstance(item, dict)
        and str(item.get("source_file") or "").strip()
        and str(item.get("chunk_id") or "").strip()
    }
    # Explicit but malformed allowlists must fail closed instead of silently
    # widening the generation input back to every retrieval result.
    if raw_allowlist is not None and (
        not isinstance(raw_allowlist, list) or not allowlist
    ):
        return []
    evidence: list[dict[str, Any]] = []
    seen_content: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("metadata_identity_valid") is False:
            continue
        source_file = str(item.get("source_file") or "").strip()
        raw_chunk_id = str(item.get("chunk_id") or "").strip()
        chunk_id = raw_chunk_id or derive_generation_chunk_id(item)
        identity = (source_file, raw_chunk_id)
        if allowlist and identity not in allowlist:
            continue
        content = str(item.get("content") or item.get("content_preview") or "").strip()
        if select_excerpts:
            content = select_generation_excerpt(
                content,
                query=str(retrieval_payload.get("query") or ""),
                heading_path=str(item.get("heading_path") or ""),
            )
        normalized = normalize_evidence_text(content)
        if not content:
            continue
        redundant_index = next(
            (
                index
                for index, previous in enumerate(seen_content)
                if evidence_texts_are_redundant(normalized, previous)
            ),
            None,
        )
        if redundant_index is not None:
            if len(normalized) <= len(seen_content[redundant_index]):
                continue
            seen_content[redundant_index] = normalized
            updated = _copy_evidence_with_content(item, content)
            updated["chunk_id"] = chunk_id
            evidence[redundant_index] = updated
            continue
        seen_content.append(normalized)
        updated = _copy_evidence_with_content(item, content)
        updated["chunk_id"] = chunk_id
        evidence.append(updated)

    active_budgeter = budgeter or DEFAULT_CONTEXT_BUDGETER
    max_chars = active_budgeter.limit(limit)
    required_sources = {
        str(source).strip().lower()
        for source in retrieval_payload.get("required_sources", []) or []
        if str(source).strip()
    }
    ordered_evidence = list(evidence)
    reserved: list[dict[str, Any]] = []
    if required_sources:
        reserved_ids: set[tuple[str, str]] = set()
        for source in sorted(required_sources):
            candidate = next(
                (
                    item
                    for item in ordered_evidence
                    if citation_source_basename(item.get("source_file")) == source
                ),
                None,
            )
            if candidate is None:
                return []
            identity = (
                str(candidate.get("source_file") or "").strip(),
                str(candidate.get("chunk_id") or "").strip(),
            )
            if identity not in reserved_ids:
                reserved.append(candidate)
                reserved_ids.add(identity)
        ordered_evidence = reserved + [
            item
            for item in ordered_evidence
            if (
                str(item.get("source_file") or "").strip(),
                str(item.get("chunk_id") or "").strip(),
            )
            not in reserved_ids
        ]

    if reserved:
        selected, used_chars = _fit_required_evidence(
            reserved,
            max_chars=max_chars,
            budgeter=active_budgeter,
        )
        if not selected:
            return []
        selected_sources = {
            citation_source_basename(item.get("source_file")) for item in selected
        }
        remaining_evidence = ordered_evidence[len(reserved) :]
    else:
        selected = []
        used_chars = 0
        selected_sources = set()
        remaining_evidence = ordered_evidence

    for item in remaining_evidence:
        source_file = str(item.get("source_file") or "未知来源").strip()
        chunk_id = str(item.get("chunk_id") or "unknown").strip()
        content = str(item.get("content") or item.get("content_preview") or "").strip()
        separator_chars = 2 if selected else 0
        header = _format_evidence_block(
            len(selected) + 1,
            source_file=source_file,
            chunk_id=chunk_id,
            content="",
        )
        remaining = max_chars - used_chars - separator_chars
        if remaining <= len(header):
            break
        if len(header) + len(content) <= remaining:
            selected.append(dict(item))
            selected_sources.add(citation_source_basename(item.get("source_file")))
            used_chars += separator_chars + len(header) + len(content)
            continue
        if selected:
            break
        marker = active_budgeter.budget.truncation_marker
        available_content = remaining - len(header)
        if available_content <= len(marker):
            break
        truncated = active_budgeter.text(
            content,
            limit=available_content,
            preserve_tail=True,
        )
        trimmed_item = dict(item)
        if item.get("content"):
            trimmed_item["content"] = truncated
        else:
            trimmed_item["content_preview"] = truncated
        selected.append(trimmed_item)
        selected_sources.add(citation_source_basename(trimmed_item.get("source_file")))
        break
    if required_sources and not required_sources.issubset(selected_sources):
        return []
    normalized_evidence = normalize_generation_citation_labels(selected)
    return [
        {
            **item,
            "citation_index": index,
        }
        for index, item in enumerate(normalized_evidence, 1)
    ]


def derive_generation_chunk_id(item: dict[str, Any]) -> str:
    """Recover a stable id for legacy runtime rows with source but no chunk id."""
    source_file = citation_source_basename(item.get("source_file"))
    if not source_file:
        return ""
    for key in ("primary_key", "row_number", "page_number", "citation_index", "rank"):
        value = str(item.get(key) or "").strip()
        if value:
            return f"{source_file}#legacy-{key}-{value}"
    content = str(item.get("content") or item.get("content_preview") or "").strip()
    if not content:
        return ""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"{source_file}#legacy-{digest}"

def _fit_required_evidence(
    evidence: list[dict[str, Any]],
    *,
    max_chars: int,
    budgeter: ContextBudgeter,
) -> tuple[list[dict[str, Any]], int]:
    """Fit one chunk per required source before admitting optional evidence."""
    if not evidence:
        return [], 0
    headers = [
        _format_evidence_block(
            index,
            source_file=str(item.get("source_file") or "未知来源").strip(),
            chunk_id=str(item.get("chunk_id") or "unknown").strip(),
            content="",
        )
        for index, item in enumerate(evidence, 1)
    ]
    separator_chars = 2 * max(len(evidence) - 1, 0)
    fixed_chars = sum(len(header) for header in headers) + separator_chars
    available_content = max_chars - fixed_chars
    marker_chars = len(budgeter.budget.truncation_marker)
    if available_content < len(evidence) * (marker_chars + 1):
        return [], 0

    contents = [
        str(item.get("content") or item.get("content_preview") or "").strip()
        for item in evidence
    ]
    allocations = _fair_content_allocations(contents, available_content)
    selected: list[dict[str, Any]] = []
    used_chars = separator_chars
    for item, header, content, allocation in zip(
        evidence,
        headers,
        contents,
        allocations,
        strict=True,
    ):
        if len(content) > allocation and allocation <= marker_chars:
            return [], 0
        rendered_content = budgeter.text(
            content,
            limit=allocation,
            preserve_tail=True,
        )
        updated = dict(item)
        if item.get("content"):
            updated["content"] = rendered_content
        else:
            updated["content_preview"] = rendered_content
        selected.append(updated)
        used_chars += len(header) + len(rendered_content)
    return selected, used_chars

def _fair_content_allocations(contents: list[str], total_chars: int) -> list[int]:
    """Share content budget fairly while returning unused quota from short chunks."""
    allocations = [0] * len(contents)
    remaining_indices = set(range(len(contents)))
    remaining_chars = max(total_chars, 0)
    while remaining_indices:
        share = remaining_chars // len(remaining_indices)
        short_indices = {
            index for index in remaining_indices if len(contents[index]) <= share
        }
        if not short_indices:
            ordered = sorted(remaining_indices)
            for offset, index in enumerate(ordered):
                allocation = share + (1 if offset < remaining_chars % len(ordered) else 0)
                allocations[index] = allocation
            break
        for index in sorted(short_indices):
            allocations[index] = len(contents[index])
            remaining_chars -= allocations[index]
            remaining_indices.remove(index)
    return allocations

def select_generation_excerpt(
    content: str,
    *,
    query: str,
    heading_path: str = "",
    target_chars: int = GENERATION_EVIDENCE_TARGET_CHARS,
) -> str:
    """Keep complete query-relevant evidence blocks instead of arbitrary chunk prefixes."""
    text = str(content or "").strip()
    if not text or len(text) <= target_chars:
        return text

    blocks = _split_evidence_blocks(text)
    if len(blocks) <= 1:
        structured_marker = re.search(
            r"\b[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^。；;\r\n]{1,240}",
            text,
        )
        query_terms = extract_retrieval_terms(f"{query} {heading_path}")
        marker_context = (
            text[max(0, structured_marker.start() - 120) : structured_marker.end() + 120]
            if structured_marker is not None
            else ""
        )
        if structured_marker is not None and (
            query_terms & extract_retrieval_terms(marker_context)
        ):
            return _center_generation_excerpt(
                text,
                marker_start=structured_marker.start(),
                marker_end=structured_marker.end(),
                target_chars=target_chars,
            )
        return DEFAULT_CONTEXT_BUDGETER.text(text, limit=target_chars, preserve_tail=True)

    query_terms = extract_retrieval_terms(f"{query} {heading_path}")
    safety_requested = any(
        marker in str(query or "").lower()
        for marker in {
            "边界",
            "审批",
            "重启",
            "扩容",
            "限流",
            "回滚",
            "清理",
            "删除",
            "截断",
            "dry-run",
        }
    )
    scored: list[tuple[float, int, str]] = []
    for index, block in enumerate(blocks):
        block_terms = extract_retrieval_terms(block)
        score = float(len(query_terms & block_terms) * 4)
        lowered = block.lower()
        if safety_requested and any(
            marker in lowered
            for marker in {"审批", "dry-run", "回滚", "人工", "不自动执行", "影响范围"}
        ):
            score += 12
        if any(
            marker in lowered
            for marker in {"命令", "检查", "确认", "验证", "证据", "指标", "日志", "selector"}
        ):
            score += 2
        if block.lstrip().startswith("#"):
            score += 0.5
        scored.append((score, index, block))

    selected_indices: set[int] = set()
    used_chars = 0
    for score, index, block in sorted(scored, key=lambda item: (-item[0], item[1])):
        if score <= 0 and selected_indices:
            continue
        separator_chars = 2 if selected_indices else 0
        if used_chars + separator_chars + len(block) > target_chars:
            continue
        selected_indices.add(index)
        used_chars += separator_chars + len(block)

    if not selected_indices:
        return DEFAULT_CONTEXT_BUDGETER.text(text, limit=target_chars, preserve_tail=True)
    return "\n\n".join(blocks[index] for index in sorted(selected_indices))


def _center_generation_excerpt(
    text: str,
    *,
    marker_start: int,
    marker_end: int,
    target_chars: int,
) -> str:
    """Keep a structured formula in a bounded excerpt from one long text block."""
    truncation_marker = DEFAULT_CONTEXT_BUDGETER.budget.truncation_marker
    content_chars = target_chars - (2 * len(truncation_marker))
    if content_chars <= 0 or marker_end - marker_start > content_chars:
        return DEFAULT_CONTEXT_BUDGETER.text(text, limit=target_chars, preserve_tail=True)
    marker_center = marker_start + ((marker_end - marker_start) // 2)
    start = max(0, marker_center - (content_chars // 2))
    end = min(len(text), start + content_chars)
    start = max(0, end - content_chars)
    prefix = truncation_marker if start else ""
    suffix = truncation_marker if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"

def _split_evidence_blocks(content: str) -> list[str]:
    """Split Markdown-like evidence while keeping fenced command examples intact."""
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in str(content or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        if not stripped and not in_fence:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]

def _copy_evidence_with_content(item: dict[str, Any], content: str) -> dict[str, Any]:
    updated = dict(item)
    if item.get("content") is not None:
        updated["content"] = content
    else:
        updated["content_preview"] = content
    return updated

def citation_source_basename(value: Any) -> str:
    """Normalize a source label for required-source and generation checks."""
    return str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1].lower()

def normalize_generation_citation_labels(
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Use basenames when unique, preserving relative paths for real collisions."""
    citation_counts: dict[tuple[str, str], int] = {}
    for item in evidence:
        source_file = str(item.get("source_file") or "").strip().replace("\\", "/")
        citation = (source_file.rsplit("/", 1)[-1], str(item.get("chunk_id") or "").strip())
        citation_counts[citation] = citation_counts.get(citation, 0) + 1

    normalized = []
    for item in evidence:
        updated = dict(item)
        source_file = str(updated.get("source_file") or "").strip().replace("\\", "/")
        basename = source_file.rsplit("/", 1)[-1]
        citation = (basename, str(updated.get("chunk_id") or "").strip())
        if citation_counts.get(citation) == 1:
            updated["source_file"] = basename
        normalized.append(updated)
    return normalized

def normalize_evidence_text(content: str) -> str:
    """Normalize formatting differences before comparing retrieved chunks."""
    return re.sub(r"\s+", " ", str(content or "")).strip().lower()

def evidence_texts_are_redundant(left: str, right: str) -> bool:
    """Treat exact and near-duplicate copies as one generation-time evidence block."""
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) < 50:
        return False
    return shorter in longer and len(shorter) / len(longer) >= 0.65

def _format_evidence_block(
    index: int,
    *,
    source_file: str,
    chunk_id: str,
    content: str,
) -> str:
    return f"[证据 {index}: source_file={source_file}; chunk_id={chunk_id}]\n{content}"
