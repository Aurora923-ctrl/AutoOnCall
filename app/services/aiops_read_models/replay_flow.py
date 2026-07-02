"""Approval and safe-change flow summaries for incident replay."""

from __future__ import annotations

from typing import Any

from app.models.approval import ApprovalRequest
from app.services.aiops_read_models.common import (
    build_approval_summary,
    latest_approval_request,
)


def build_replay_approval_flow(approvals: list[ApprovalRequest]) -> dict[str, Any]:
    """Build the approval section used by the replay workbench."""
    latest = latest_approval_request(approvals)
    summary = build_approval_summary(approvals, latest)
    latest_payload = latest.model_dump(mode="json") if latest else {}
    before = (
        f"等待人工审批：{latest.action}"
        if latest and latest.status == "pending"
        else f"触发审批：{latest.action}"
        if latest
        else "未触发审批"
    )
    after = replay_approval_after_text(latest)
    return {
        "summary": summary,
        "items": [approval.model_dump(mode="json") for approval in approvals],
        "before_after": {
            "before": before,
            "after": after,
            "action": latest_payload.get("action", ""),
            "decision_reason": latest_payload.get("decision_reason")
            or latest_payload.get("reason", ""),
            "approved_to_continue": bool(latest and latest.status == "approved"),
        },
    }


def build_replay_change_flow(change_executions: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the safe-change section used by the replay workbench."""
    latest = change_executions[-1] if change_executions else {}
    return {
        "total": len(change_executions),
        "status": str(latest.get("lifecycle_status") or latest.get("status") or "not_started"),
        "latest": latest or None,
        "items": change_executions,
    }


def replay_approval_stage_status(approvals: list[ApprovalRequest]) -> str:
    """Return the status used by the approval stage card."""
    if not approvals:
        return "not_required"
    return str(build_approval_summary(approvals).get("status") or "not_required")


def replay_approval_stage_summary(approvals: list[ApprovalRequest]) -> str:
    """Return the summary used by the approval stage card."""
    latest = latest_approval_request(approvals)
    if latest is None:
        return "未触发审批"
    if latest.status == "pending":
        return f"等待审批：{latest.action}"
    return f"审批{latest.status}：{latest.decision_reason or latest.reason or latest.action}"


def replay_approval_after_text(approval: ApprovalRequest | None) -> str:
    """Return human readable post-approval state."""
    if approval is None:
        return "可继续只读诊断或直接进入报告。"
    if approval.status == "pending":
        return "审批未完成，后续高风险动作暂停。"
    if approval.status == "approved":
        return "审批通过，可进入受控恢复或安全变更流程。"
    if approval.status == "rejected":
        return "审批拒绝，相关变更动作不得继续执行。"
    return f"审批状态：{approval.status}"
