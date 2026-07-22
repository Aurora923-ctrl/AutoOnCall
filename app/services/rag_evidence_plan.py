"""Plan and freeze the exact retrieval evidence exposed to answer generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from app.services.context_budget import DEFAULT_CONTEXT_BUDGETER, ContextBudgeter
from app.services.rag_generation_context import (
    citation_source_basename,
    select_generation_excerpt,
)
from app.services.rag_question_plan import AnswerSubgoal, QuestionPlan


@dataclass(frozen=True, slots=True)
class EvidenceBinding:
    subgoal_id: str
    source_file: str
    chunk_id: str
    source_role: str
    matched_entities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FrozenGenerationEvidence:
    items: tuple[dict[str, Any], ...]
    bindings: tuple[EvidenceBinding, ...]
    missing_subgoals: tuple[str, ...]
    missing_entities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PlannedCandidate:
    item: dict[str, Any]
    source_role: str
    matched_entities: tuple[str, ...]
    matched_subgoals: tuple[str, ...]

    @property
    def identity(self) -> tuple[str, str]:
        return (
            str(self.item.get("source_file") or "").strip(),
            str(self.item.get("chunk_id") or "").strip(),
        )


def classify_source_role(item: dict[str, Any]) -> str:
    """Classify static retrieval evidence without implying live/current state."""
    source_file = str(item.get("source_file") or "").strip().replace("\\", "/")
    filename = PurePath(source_file).name.casefold()
    metadata = item.get("metadata")
    snapshot_type = str(
        item.get("snapshot_type")
        or (metadata.get("snapshot_type") if isinstance(metadata, dict) else "")
        or ""
    ).casefold()
    metadata_values = " ".join(
        str(value).casefold()
        for value in (
            item.get("source_role"),
            item.get("source_type"),
            item.get("document_type"),
            metadata.get("source_role") if isinstance(metadata, dict) else None,
            metadata.get("source_type") if isinstance(metadata, dict) else None,
            metadata.get("document_type") if isinstance(metadata, dict) else None,
            metadata.get("doc_type") if isinstance(metadata, dict) else None,
            metadata.get("snapshot_type") if isinstance(metadata, dict) else None,
        )
        if value is not None
    )

    if filename.endswith(".pdf") or "postmortem" in filename or "postmortem" in metadata_values:
        return "postmortem"
    if (
        filename.endswith((".csv", ".xls", ".xlsx"))
        or "ticket" in filename
        or any(marker in metadata_values for marker in ("ticket", "table", "spreadsheet"))
    ):
        return "ticket"
    if (
        filename.startswith("official_")
        or snapshot_type in {"official", "official_snapshot"}
        or ("official" in metadata_values and "snapshot" in metadata_values)
    ):
        return "official"
    return "runbook"


def build_frozen_generation_evidence(
    plan: QuestionPlan,
    retrieval_payload: dict[str, Any],
    *,
    budgeter: ContextBudgeter | None = None,
    limit: int | None = None,
) -> FrozenGenerationEvidence:
    """Select, bind, budget, and freeze generation evidence exactly once."""
    empty = _empty_evidence(plan)
    if retrieval_payload.get("status") not in (None, "success"):
        return empty

    candidates = _build_candidates(plan, retrieval_payload)
    if not candidates:
        return empty

    required_roles = _required_roles(plan)
    required_sources = tuple(
        dict.fromkeys(
            citation_source_basename(source)
            for source in retrieval_payload.get("required_sources", ()) or ()
            if str(source or "").strip()
        )
    )
    required: list[_PlannedCandidate] = []
    required_ids: set[tuple[str, str]] = set()

    for role in required_roles:
        candidate = next((item for item in candidates if item.source_role == role), None)
        if candidate is None:
            return empty
        _append_candidate(required, required_ids, candidate)

    for source in required_sources:
        candidate = next(
            (
                item
                for item in candidates
                if citation_source_basename(item.item.get("source_file")) == source
            ),
            None,
        )
        if candidate is None:
            return empty
        _append_candidate(required, required_ids, candidate)

    active_budgeter = budgeter or DEFAULT_CONTEXT_BUDGETER
    max_chars = active_budgeter.limit(limit)
    if _rendered_length(required) > max_chars:
        return empty

    selected = list(required)
    selected_ids = set(required_ids)
    covered_subgoals = _covered_subgoals(selected)
    covered_entities = _covered_entities(selected)

    for subgoal in plan.subgoals:
        if subgoal.id in covered_subgoals:
            continue
        candidate = next(
            (
                item
                for item in candidates
                if item.identity not in selected_ids and subgoal.id in item.matched_subgoals
            ),
            None,
        )
        if candidate is not None and _fits(selected, candidate, max_chars=max_chars):
            _append_candidate(selected, selected_ids, candidate)
            covered_subgoals.update(candidate.matched_subgoals)
            covered_entities.update(entity.casefold() for entity in candidate.matched_entities)

    for entity in plan.explicit_entities:
        if entity.casefold() in covered_entities:
            continue
        candidate = next(
            (
                item
                for item in candidates
                if item.identity not in selected_ids
                and entity.casefold()
                in {matched.casefold() for matched in item.matched_entities}
            ),
            None,
        )
        if candidate is not None and _fits(selected, candidate, max_chars=max_chars):
            _append_candidate(selected, selected_ids, candidate)
            covered_subgoals.update(candidate.matched_subgoals)
            covered_entities.update(matched.casefold() for matched in candidate.matched_entities)

    for candidate in candidates:
        if candidate.identity in selected_ids:
            continue
        if _fits(selected, candidate, max_chars=max_chars):
            _append_candidate(selected, selected_ids, candidate)

    items = tuple(
        {
            **candidate.item,
            "citation_index": index,
        }
        for index, candidate in enumerate(selected, 1)
    )
    bindings = tuple(
        EvidenceBinding(
            subgoal_id=subgoal_id,
            source_file=str(candidate.item.get("source_file") or ""),
            chunk_id=str(candidate.item.get("chunk_id") or ""),
            source_role=candidate.source_role,
            matched_entities=candidate.matched_entities,
        )
        for candidate in selected
        for subgoal_id in candidate.matched_subgoals
    )
    bound_subgoals = {binding.subgoal_id for binding in bindings}
    matched_entities = {
        entity.casefold() for binding in bindings for entity in binding.matched_entities
    }
    return FrozenGenerationEvidence(
        items=items,
        bindings=bindings,
        missing_subgoals=tuple(
            subgoal.id for subgoal in plan.subgoals if subgoal.id not in bound_subgoals
        ),
        missing_entities=tuple(
            entity for entity in plan.explicit_entities if entity.casefold() not in matched_entities
        ),
    )


def _build_candidates(
    plan: QuestionPlan,
    retrieval_payload: dict[str, Any],
) -> list[_PlannedCandidate]:
    results = retrieval_payload.get("retrieval_results")
    if not isinstance(results, list):
        return []
    allowlist = _generation_allowlist(retrieval_payload)
    if allowlist is False:
        return []

    candidates: list[_PlannedCandidate] = []
    for raw_item in results:
        if not isinstance(raw_item, dict) or raw_item.get("metadata_identity_valid") is False:
            continue
        source_file = str(raw_item.get("source_file") or "").strip()
        raw_chunk_id = str(raw_item.get("chunk_id") or "").strip()
        if isinstance(allowlist, set) and (source_file, raw_chunk_id) not in allowlist:
            continue
        chunk_id = raw_chunk_id
        raw_content = str(
            raw_item.get("content") or raw_item.get("content_preview") or ""
        ).strip()
        if not source_file or not chunk_id or not raw_content:
            continue
        content = select_generation_excerpt(
            raw_content,
            query=plan.query,
            heading_path=str(raw_item.get("heading_path") or ""),
        )
        if not content:
            continue
        item = {
            **raw_item,
            "source_file": source_file,
            "chunk_id": chunk_id,
            "content": content,
        }
        role = classify_source_role(item)
        searchable = " ".join(
            (
                source_file,
                str(item.get("heading_path") or ""),
                content,
            )
        ).casefold()
        matched_entities = tuple(
            entity for entity in plan.explicit_entities if entity.casefold() in searchable
        )
        matched_subgoals = tuple(
            subgoal.id
            for subgoal in plan.subgoals
            if _matches_subgoal(subgoal, searchable, role, matched_entities)
        )
        candidates.append(
            _PlannedCandidate(
                item=item,
                source_role=role,
                matched_entities=matched_entities,
                matched_subgoals=matched_subgoals,
            )
        )
    return candidates


def _generation_allowlist(
    retrieval_payload: dict[str, Any],
) -> set[tuple[str, str]] | None | bool:
    raw_allowlist = retrieval_payload.get("generation_allowlist")
    if raw_allowlist is None:
        return None
    if not isinstance(raw_allowlist, list):
        return False
    allowlist = {
        (
            str(item.get("source_file") or "").strip(),
            str(item.get("chunk_id") or "").strip(),
        )
        for item in raw_allowlist
        if isinstance(item, dict)
        and str(item.get("source_file") or "").strip()
        and str(item.get("chunk_id") or "").strip()
    }
    return allowlist or False


def _required_roles(plan: QuestionPlan) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            role
            for subgoal in plan.subgoals
            for role in subgoal.required_source_roles
            if role
        )
    )


def _matches_subgoal(
    subgoal: AnswerSubgoal,
    searchable: str,
    source_role: str,
    matched_entities: tuple[str, ...],
) -> bool:
    if source_role in subgoal.required_source_roles or matched_entities:
        return True
    markers = {
        "evidence": ("检查", "证据", "指标", "日志", "命令", "check", "metric"),
        "diagnosis": ("判断", "原因", "根因", "容量", "risk", "retry", "diagnos"),
        "alert_design": ("告警", "症状", "alert", "symptom"),
        "boundary": ("审批", "回滚", "dry-run", "只读", "approval", "rollback"),
        "temporal_boundary": ("历史", "当前", "事故", "复盘", "incident", "postmortem"),
    }
    if subgoal.id == "temporal_boundary" and source_role in {"postmortem", "ticket"}:
        return True
    return any(marker in searchable for marker in markers.get(subgoal.id, ()))


def _append_candidate(
    selected: list[_PlannedCandidate],
    selected_ids: set[tuple[str, str]],
    candidate: _PlannedCandidate,
) -> None:
    if candidate.identity not in selected_ids:
        selected.append(candidate)
        selected_ids.add(candidate.identity)


def _covered_subgoals(candidates: list[_PlannedCandidate]) -> set[str]:
    return {subgoal for candidate in candidates for subgoal in candidate.matched_subgoals}


def _covered_entities(candidates: list[_PlannedCandidate]) -> set[str]:
    return {
        entity.casefold() for candidate in candidates for entity in candidate.matched_entities
    }


def _fits(
    selected: list[_PlannedCandidate],
    candidate: _PlannedCandidate,
    *,
    max_chars: int,
) -> bool:
    return _rendered_length([*selected, candidate]) <= max_chars


def _rendered_length(candidates: list[_PlannedCandidate]) -> int:
    return len(
        "\n\n".join(
            "[证据 "
            f"{index}: source_file={str(candidate.item.get('source_file') or '未知来源').strip()}; "
            f"chunk_id={str(candidate.item.get('chunk_id') or 'unknown').strip()}]\n"
            f"{str(candidate.item.get('content') or candidate.item.get('content_preview') or '')}"
            for index, candidate in enumerate(candidates, 1)
        )
    )


def _empty_evidence(plan: QuestionPlan) -> FrozenGenerationEvidence:
    return FrozenGenerationEvidence(
        items=(),
        bindings=(),
        missing_subgoals=tuple(subgoal.id for subgoal in plan.subgoals),
        missing_entities=plan.explicit_entities,
    )
