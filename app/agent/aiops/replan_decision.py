"""Structured Replanner decision normalization and safety validation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field

from app.models.plan import PlanStep
from app.tools.registry import create_default_tool_registry

from .evidence_analyzer import EvidenceAnalysis
from .risk_controller import assess_plan_step
from .state import PlanExecuteState

LLM_DECISION_SAFE_SKIP_DECISIONS = {"retry_failed_tool", "request_approval"}
MAX_REPLAN_STEPS = 8


class ReplanDecision(BaseModel):
    """Structured Replanner decision."""

    decision: Literal[
        "continue_investigation",
        "add_steps",
        "retry_failed_tool",
        "request_approval",
        "generate_report",
        "escalate_to_human",
    ]
    reason: str = Field(default="", description="Decision reason")
    new_steps: list[PlanStep] = Field(default_factory=list)


def decision_from_analysis(analysis: EvidenceAnalysis) -> ReplanDecision:
    """Normalize EvidenceAnalysis into the Replanner decision contract."""
    new_steps: list[PlanStep] = []
    if analysis.decision == "retry_failed_tool":
        new_steps = analysis.retry_steps
    elif analysis.decision == "add_steps":
        new_steps = analysis.recommended_steps
    return ReplanDecision(
        decision=analysis.decision,
        reason=analysis.reason,
        new_steps=new_steps,
    )


async def decide_with_llm_or_analysis(
    state: PlanExecuteState,
    analysis: EvidenceAnalysis,
    analysis_decision: ReplanDecision,
    *,
    llm_enabled: bool,
    generate_llm_decision: Callable[
        [PlanExecuteState, EvidenceAnalysis, ReplanDecision], Awaitable[Any]
    ],
) -> tuple[ReplanDecision, str]:
    """Let an optional structured LLM critic refine the deterministic decision."""
    if not llm_enabled:
        return analysis_decision, "evidence_analyzer"
    if analysis_decision.decision in LLM_DECISION_SAFE_SKIP_DECISIONS:
        return analysis_decision, "evidence_analyzer_safety_priority"
    try:
        llm_decision = await generate_llm_decision(state, analysis, analysis_decision)
        normalized = normalize_llm_replan_decision(
            llm_decision,
            state,
            analysis,
            analysis_decision,
        )
        if normalized is not None:
            return normalized, "llm_structured"
    except Exception as exc:
        logger.warning(f"Replanner LLM decision unavailable; using Evidence Analyzer: {exc}")
    return analysis_decision, "evidence_analyzer_fallback"


def normalize_llm_replan_decision(
    decision_obj: Any,
    state: PlanExecuteState,
    analysis: EvidenceAnalysis,
    analysis_decision: ReplanDecision,
) -> ReplanDecision | None:
    """Validate LLM output against deterministic evidence and risk gates."""
    decision = _coerce_replan_decision(decision_obj)
    if decision is None:
        return None
    reason = (decision.reason or "").strip() or analysis_decision.reason
    if decision.decision == "generate_report" and not analysis.evidence_sufficient:
        return None
    if decision.decision == "continue_investigation" and not _has_remaining_plan(state):
        return None
    if decision.decision == "retry_failed_tool" and not _has_failed_tool_record(state):
        return None
    if decision.decision in {"add_steps", "retry_failed_tool"}:
        steps = _coerce_plan_steps(decision.new_steps)
        if not steps:
            return None
        safe_steps = _safe_llm_steps_or_none(
            steps,
            state,
            retry=decision.decision == "retry_failed_tool",
        )
        if safe_steps is None:
            return None
        return ReplanDecision(
            decision=decision.decision,
            reason=reason,
            new_steps=safe_steps[:MAX_REPLAN_STEPS],
        )
    return ReplanDecision(decision=decision.decision, reason=reason, new_steps=[])


def _coerce_replan_decision(decision_obj: Any) -> ReplanDecision | None:
    if isinstance(decision_obj, ReplanDecision):
        return decision_obj
    if isinstance(decision_obj, dict):
        try:
            return ReplanDecision(**decision_obj)
        except Exception:
            return None
    return None


def _coerce_plan_steps(raw_steps: list[Any]) -> list[PlanStep]:
    steps: list[PlanStep] = []
    for raw_step in raw_steps:
        try:
            step = raw_step if isinstance(raw_step, PlanStep) else PlanStep(**raw_step)
        except Exception:
            continue
        steps.append(step.model_copy(update={"status": "pending"}))
    return steps


def _safe_llm_steps_or_none(
    steps: list[PlanStep],
    state: PlanExecuteState,
    *,
    retry: bool,
) -> list[PlanStep] | None:
    registry = create_default_tool_registry([])
    safe_steps: list[PlanStep] = []
    for step in steps:
        if step.tool_name != "manual_analysis" and registry.get(step.tool_name) is None:
            return None
        normalized_step = step.model_copy(
            update={
                "status": "pending",
                "retry_count": max(step.retry_count, 1) if retry else 0,
            }
        )
        risk_decision = assess_plan_step(
            normalized_step,
            tool_registry=registry,
            incident=state.get("incident"),
        )
        if risk_decision.policy != "allow":
            return None
        safe_steps.append(normalized_step)
    return safe_steps


def _has_remaining_plan(state: PlanExecuteState) -> bool:
    return bool(state.get("current_plan") or state.get("plan"))


def _has_failed_tool_record(state: PlanExecuteState) -> bool:
    for record in state.get("tool_call_records", []):
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "").lower()
        step_id = str(record.get("step_id") or "")
        if status in {"failed", "error"} and not step_id.endswith("-retry"):
            return True
    return False
