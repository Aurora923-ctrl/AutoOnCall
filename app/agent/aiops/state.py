"""
通用 Plan-Execute-Replan 状态定义
基于 LangGraph 官方教程实现
"""

import operator
from collections.abc import Iterable
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from app.models.incident import Incident
from app.models.plan import PlanStep

from .plan_fallback import render_plan_step


class PlanExecuteState(TypedDict, total=False):
    """Plan-Execute-Replan 状态"""

    input: str
    session_id: str

    plan: list[str]

    past_steps: Annotated[list[tuple], operator.add]

    response: str

    # 结构化故障事件。保持 dict 形态，避免 checkpointer/序列化对 Pydantic 对象产生兼容问题。
    incident: dict[str, Any]

    # current_plan 是主计划队列；legacy plan 仅用于 SSE 和兼容路径。
    current_plan: list[dict[str, Any]]

    executed_steps: Annotated[list[dict[str, Any]], operator.add]

    # Executor 将 Tool Registry 调用沉淀为 ToolCallRecord，便于 trace 查询和故障回放。
    tool_call_records: Annotated[list[dict[str, Any]], operator.add]

    # 工具调用结果沉淀为 Evidence；past_steps 只保留 legacy 文本历史。
    gathered_evidence: Annotated[list[dict[str, Any]], operator.add]

    # 根因假设
    hypotheses: list[str]

    # 证据分析结果，由 Evidence Analyzer 写入，Report Generator 和 Trace 使用。
    evidence_analysis: dict[str, Any] | None

    # 风险评估与人工审批
    risk_assessment: dict[str, Any] | None
    pending_approval: dict[str, Any] | None
    change_plan: dict[str, Any] | None

    # 最终诊断和修复建议
    final_diagnosis: str
    remediation_suggestion: str
    report: dict[str, Any] | None

    # 错误列表和全链路 trace
    errors: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]
    trace_id: str


def parse_plan_step(raw_step: Any) -> PlanStep | None:
    """Parse a JSON-safe PlanStep from state, returning None for legacy values."""
    try:
        if isinstance(raw_step, PlanStep):
            return raw_step
        if isinstance(raw_step, dict):
            return PlanStep(**raw_step)
    except Exception:
        return None
    return None


def normalize_plan_state_update(steps: Iterable[PlanStep]) -> dict[str, list[Any]]:
    """Return synchronized canonical and legacy plan queues."""
    step_list = list(steps)
    return {
        "current_plan": [step.model_dump(mode="json") for step in step_list],
        "plan": [render_plan_step(step) for step in step_list],
    }


def remaining_plan_state_update(
    current_plan: list[dict[str, Any]],
    legacy_plan: list[str],
    *,
    consumed: int = 1,
) -> dict[str, list[Any]]:
    """Consume canonical plan first and derive legacy plan from it when possible."""
    if current_plan:
        remaining_raw = current_plan[consumed:]
        remaining_steps = [parse_plan_step(step) for step in remaining_raw]
        if all(step is not None for step in remaining_steps):
            return normalize_plan_state_update(step for step in remaining_steps if step is not None)
        return {
            "current_plan": remaining_raw,
            "plan": legacy_plan[consumed:] if legacy_plan else [],
        }
    return {
        "current_plan": [],
        "plan": legacy_plan[consumed:] if legacy_plan else [],
    }


def create_initial_aiops_state(
    user_input: str,
    session_id: str | None = None,
    incident: Incident | None = None,
) -> PlanExecuteState:
    """Create a backward-compatible initial LangGraph state."""
    session_id = session_id or f"session-{uuid4().hex}"
    incident_obj = incident or Incident(
        title="AIOps diagnosis request",
        service_name="unknown-service",
        severity="P2",
        symptom=user_input[:500],
        raw_alert={
            "session_id": session_id,
            "input": user_input,
        },
    )

    return {
        # Legacy fields consumed by the current Planner/Executor/Replanner.
        "input": user_input,
        "session_id": session_id,
        "plan": [],
        "past_steps": [],
        "response": "",
        # New industrial-grade state fields. They are intentionally additive.
        "incident": incident_obj.model_dump(mode="json"),
        "current_plan": [],
        "executed_steps": [],
        "tool_call_records": [],
        "gathered_evidence": [],
        "hypotheses": [],
        "evidence_analysis": None,
        "risk_assessment": None,
        "pending_approval": None,
        "change_plan": None,
        "final_diagnosis": "",
        "remediation_suggestion": "",
        "report": None,
        "errors": [],
        "warnings": [],
        "trace_id": f"trace-{uuid4().hex}",
    }
