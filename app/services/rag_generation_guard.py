"""Shared preparation and citation gates for streaming and non-streaming RAG answers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.services.rag_answer_contract import AnswerContract, build_answer_contract
from app.services.rag_answer_coverage import answer_topic_focus
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
    repair_missing_claim_citations,
    select_supporting_citations,
)
from app.services.rag_evidence_plan import (
    FrozenGenerationEvidence,
    build_frozen_generation_evidence,
)
from app.services.rag_generation_context import citation_source_basename
from app.services.rag_question_plan import QuestionPlan, build_question_plan
from app.services.rag_read_models import build_citations, compact_retrieval_payload


@dataclass(frozen=True, slots=True)
class GenerationPreparation:
    generation_payload: dict[str, Any] | None
    citations: list[dict[str, Any]]
    refusal_answer: str = ""
    refusal_context: dict[str, Any] | None = None
    refusal_policy: str = ""
    frozen_evidence: FrozenGenerationEvidence | None = None
    answer_contract: AnswerContract | None = None

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
    question_plan = build_question_plan(str(retrieval_payload.get("query") or ""))
    bounded_evidence = build_generation_evidence(
        retrieval_payload,
        select_excerpts=False,
    )
    planning_payload = {
        **retrieval_payload,
        "retrieval_results": bounded_evidence,
        "generation_allowlist": [
            {
                "source_file": str(item.get("source_file") or ""),
                "chunk_id": str(item.get("chunk_id") or ""),
            }
            for item in bounded_evidence
        ],
    }
    frozen_evidence = build_frozen_generation_evidence(
        question_plan,
        planning_payload,
    )
    generation_evidence = list(frozen_evidence.items)
    answer_contract = build_answer_contract(question_plan, frozen_evidence)
    coverage = _build_frozen_answer_coverage(question_plan, frozen_evidence)
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
            frozen_evidence=frozen_evidence,
            answer_contract=answer_contract,
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
        "answer_coverage": coverage,
        "_frozen_generation_evidence": frozen_evidence,
        "_answer_contract": answer_contract,
    }
    citations = build_citations(generation_payload)
    if has_valid_citations(citations):
        return GenerationPreparation(
            generation_payload=generation_payload,
            citations=citations,
            frozen_evidence=frozen_evidence,
            answer_contract=answer_contract,
        )

    guarded_payload = build_citation_guard_payload(retrieval_payload)
    return GenerationPreparation(
        generation_payload=None,
        citations=[],
        refusal_answer=build_missing_citation_message(),
        refusal_context=compact_retrieval_payload(guarded_payload),
        refusal_policy="refuse_without_citation",
        frozen_evidence=frozen_evidence,
        answer_contract=answer_contract,
    )


def _build_frozen_answer_coverage(
    question_plan: QuestionPlan,
    frozen_evidence: FrozenGenerationEvidence,
) -> dict[str, Any]:
    """Render the compatibility coverage payload from the already-built plan."""
    labels = {
        "evidence": "需要哪些证据",
        "diagnosis": "需要哪些判断",
        "alert_design": "需要怎样设计症状告警",
        "boundary": "是否要求处置边界",
        "temporal_boundary": "是否要求区分历史与当前事件",
    }
    subgoals: list[dict[str, Any]] = []
    for subgoal in question_plan.subgoals:
        bindings = [
            binding
            for binding in frozen_evidence.bindings
            if binding.subgoal_id == subgoal.id
        ]
        bound_chunks = [
            {
                "source_file": source_file,
                "chunk_id": chunk_id,
                "matched_terms": list(matched_entities),
                "binding_reason": "frozen_evidence_binding",
            }
            for source_file, chunk_id, matched_entities in dict.fromkeys(
                (
                    binding.source_file,
                    binding.chunk_id,
                    binding.matched_entities,
                )
                for binding in bindings
            )
        ]
        subgoals.append(
            {
                "id": subgoal.id,
                "label": labels.get(subgoal.id, subgoal.id),
                "required": True,
                "bound_chunks": bound_chunks[:5],
                "covered": bool(bindings),
            }
        )
    required = len(subgoals)
    covered = sum(1 for item in subgoals if item["covered"])
    return {
        "query": question_plan.query,
        "subgoals": subgoals,
        "required_count": required,
        "covered_count": covered,
        "coverage_rate": round(covered / required, 4) if required else 1.0,
        "complete": covered == required,
        "uncovered_subgoals": [item["id"] for item in subgoals if not item["covered"]],
        "question_plan": {
            "domain": question_plan.domain,
            "explicit_entities": question_plan.explicit_entities,
            "max_claims": question_plan.max_claims,
        },
    }


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
    normalized = remove_citations_from_evidence_gap_lines(normalized)
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
        repaired = repair_missing_claim_citations(
            normalized,
            citations,
            evidence_for_guard,
        )
        if repaired != normalized:
            normalized = repaired
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

    focus = answer_topic_focus(
        str(retrieval_payload.get("query") or ""),
        normalized,
    )
    if not focus["focused"]:
        guarded_payload = build_citation_guard_payload(retrieval_payload)
        return GroundedAnswerDecision(
            answer=(
                "当前生成内容未能聚焦到用户问题的故障对象，已拒绝输出；"
                "请重新检索并生成与当前问题直接相关的答案。"
            ),
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


def missing_required_citation_sources(
    citations: list[dict[str, Any]],
    required_sources: list[str] | set[str] | tuple[str, ...] | None,
) -> list[str]:
    """Return required sources that did not contribute a cited answer claim."""
    required = {
        citation_source_basename(source)
        for source in required_sources or ()
        if str(source or "").strip()
    }
    cited = {
        citation_source_basename(item.get("source_file"))
        for item in citations
        if isinstance(item, dict) and str(item.get("source_file") or "").strip()
    }
    return sorted(required - cited)


def remove_citations_from_evidence_gap_lines(answer: str) -> str:
    """Keep explicit evidence gaps uncited instead of rejecting an otherwise grounded answer."""
    lines: list[str] = []
    for line in str(answer or "").splitlines():
        normalized = line.lstrip(" -*0123456789.)").strip()
        if normalized.startswith("当前证据不足"):
            line = re.sub(r"\s*\[[^\[\]\r\n]+\]\s*[。.!！?？]?\s*$", "", line).rstrip()
        lines.append(line)
    return "\n".join(lines).strip()
