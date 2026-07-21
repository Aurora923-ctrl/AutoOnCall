"""Shared preparation and citation gates for streaming and non-streaming RAG answers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.rag_answer_policy import (
    build_citation_guard_payload,
    build_generation_evidence,
    build_missing_citation_message,
    build_no_answer_message,
    compress_grounded_answer,
    ensure_citation_block,
    has_valid_citations,
    is_explicit_knowledge_refusal,
    remove_generic_uncertainty_boilerplate,
    select_supporting_citations,
)
from app.services.rag_read_models import build_citations, compact_retrieval_payload


@dataclass(frozen=True, slots=True)
class GenerationPreparation:
    generation_payload: dict[str, Any] | None
    citations: list[dict[str, Any]]
    refusal_answer: str = ""
    refusal_context: dict[str, Any] | None = None
    refusal_policy: str = ""

    @property
    def refused(self) -> bool:
        return self.generation_payload is None


@dataclass(frozen=True, slots=True)
class GroundedAnswerDecision:
    answer: str
    citations: list[dict[str, Any]]
    no_answer: bool
    answer_policy: str
    retrieval_context: dict[str, Any]


def prepare_grounded_generation(
    retrieval_payload: dict[str, Any],
) -> GenerationPreparation:
    """Build bounded generation evidence and apply the pre-generation citation gate."""
    generation_evidence = build_generation_evidence(
        retrieval_payload,
        select_excerpts=False,
    )
    if retrieval_payload.get("required_sources") and not generation_evidence:
        guarded_payload = build_citation_guard_payload(retrieval_payload)
        return GenerationPreparation(
            generation_payload=None,
            citations=[],
            refusal_answer=build_no_answer_message(
                {
                    **retrieval_payload,
                    "status": "no_answer",
                    "summary": (
                        "Required source coverage could not fit inside the generation "
                        "context budget."
                    ),
                    "missing_required_sources": retrieval_payload.get(
                        "required_sources",
                        [],
                    ),
                }
            ),
            refusal_context=compact_retrieval_payload(guarded_payload),
            refusal_policy="refuse_without_trusted_source",
        )

    generation_payload = {
        **retrieval_payload,
        "retrieval_results": generation_evidence,
        "generation_allowlist": [
            {
                "source_file": str(item.get("source_file") or ""),
                "chunk_id": str(item.get("chunk_id") or ""),
            }
            for item in generation_evidence
        ],
    }
    citations = build_citations(generation_payload)
    if has_valid_citations(citations):
        return GenerationPreparation(
            generation_payload=generation_payload,
            citations=citations,
        )

    guarded_payload = build_citation_guard_payload(retrieval_payload)
    return GenerationPreparation(
        generation_payload=None,
        citations=[],
        refusal_answer=build_missing_citation_message(),
        refusal_context=compact_retrieval_payload(guarded_payload),
        refusal_policy="refuse_without_citation",
    )


def finalize_grounded_answer(
    answer: str,
    citations: list[dict[str, Any]],
    retrieval_payload: dict[str, Any],
    retrieval_context: dict[str, Any],
    *,
    evidence: list[dict[str, Any]] | None = None,
    normalize_answer: bool = True,
) -> GroundedAnswerDecision:
    """Apply identical cleanup and citation checks to the frozen model evidence."""
    normalized = (
        compress_grounded_answer(remove_generic_uncertainty_boilerplate(answer))
        if normalize_answer
        else answer
    )
    if is_explicit_knowledge_refusal(normalized):
        return GroundedAnswerDecision(
            answer=build_no_answer_message(
                {
                    **retrieval_payload,
                    "status": "no_answer",
                    "summary": "当前知识库没有足够的相关证据回答该问题。",
                }
            ),
            citations=[],
            no_answer=True,
            answer_policy="refuse_without_trusted_source",
            retrieval_context=retrieval_context,
        )

    evidence_for_guard = (
        evidence
        if evidence is not None
        else [
            item
            for item in retrieval_payload.get("retrieval_results", []) or []
            if isinstance(item, dict)
        ]
    )
    supporting = select_supporting_citations(
        normalized,
        citations,
        evidence=evidence_for_guard,
    )
    if not has_valid_citations(supporting):
        guarded_payload = build_citation_guard_payload(retrieval_payload)
        return GroundedAnswerDecision(
            answer=build_missing_citation_message(),
            citations=[],
            no_answer=True,
            answer_policy="refuse_without_citation",
            retrieval_context=compact_retrieval_payload(guarded_payload),
        )

    return GroundedAnswerDecision(
        answer=ensure_citation_block(normalized, supporting),
        citations=supporting,
        no_answer=False,
        answer_policy=str(
            retrieval_payload.get("answer_policy") or "answer_with_citations"
        ),
        retrieval_context=retrieval_context,
    )
