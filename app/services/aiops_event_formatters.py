"""SSE event formatting helpers for AIOps workflow nodes."""

from __future__ import annotations

from typing import Any


def format_planner_event(state: dict[str, Any] | None) -> dict[str, Any]:
    """Format a Planner node update as a stable SSE payload."""
    if not state:
        return {"type": "status", "stage": "planner", "message": "规划节点执行中"}

    plan = state.get("plan", [])
    current_plan = state.get("current_plan", [])

    return {
        "type": "plan",
        "stage": "plan_created",
        "message": f"执行计划已制定，共 {len(plan)} 个步骤",
        "plan": plan,
        "current_plan": current_plan,
    }


def format_executor_event(state: dict[str, Any] | None) -> dict[str, Any]:
    """Format an Executor node update as a stable SSE payload."""
    if not state:
        return {"type": "status", "stage": "executor", "message": "执行节点运行中"}

    plan = state.get("plan", [])
    past_steps = state.get("past_steps", [])
    gathered_evidence = state.get("gathered_evidence", [])
    tool_call_records = state.get("tool_call_records", [])
    errors = state.get("errors", [])
    warnings = state.get("warnings", [])
    pending_approval = state.get("pending_approval")

    if pending_approval:
        return {
            "type": "approval_required",
            "stage": "approval_required",
            "message": "后续动作需要人工审批",
            "pending_approval": pending_approval,
            "risk_assessment": state.get("risk_assessment"),
            "structured_report": state.get("report"),
            "warnings": warnings,
        }

    if past_steps:
        last_step, result = past_steps[-1]
        result_text = str(result)
        return {
            "type": "step_complete",
            "stage": "step_executed",
            "message": f"步骤执行完成 ({len(past_steps)}/{len(past_steps) + len(plan)})",
            "current_step": last_step,
            "result_preview": result_text[:500],
            "remaining_steps": len(plan),
            "evidence": gathered_evidence,
            "tool_call_records": tool_call_records,
            "errors": errors,
            "warnings": warnings,
        }
    return {"type": "status", "stage": "executor", "message": "开始执行步骤"}


def format_replanner_event(state: dict[str, Any] | None) -> dict[str, Any]:
    """Format a Replanner node update as a stable SSE payload."""
    if not state:
        return {"type": "status", "stage": "replanner", "message": "评估节点运行中"}

    pending_approval = state.get("pending_approval")
    response = state.get("response", "")
    plan = state.get("current_plan") or state.get("plan", [])
    structured_report = state.get("report")
    warnings = state.get("warnings", [])

    if pending_approval:
        return {
            "type": "approval_required",
            "stage": "approval_required",
            "message": "后续动作需要人工审批",
            "pending_approval": pending_approval,
            "risk_assessment": state.get("risk_assessment"),
            "structured_report": structured_report,
            "warnings": warnings,
        }

    if response:
        return {
            "type": "report",
            "stage": "final_report",
            "message": "最终报告已生成",
            "report": response,
            "structured_report": structured_report,
            "hypotheses": state.get("hypotheses", []),
            "final_diagnosis": state.get("final_diagnosis", ""),
            "warnings": warnings,
        }
    return {
        "type": "status",
        "stage": "replanner",
        "message": f"评估完成，{'继续执行剩余步骤' if plan else '准备生成最终响应'}",
        "remaining_steps": len(plan),
        "hypotheses": state.get("hypotheses", []),
        "final_diagnosis": state.get("final_diagnosis", ""),
        "warnings": warnings,
    }
