"""Helpers for rebuilding diagnosis reports after approval resume."""

from app.models.approval import ApprovalRequest
from app.models.report import DiagnosisReport


def _build_persisted_resume_report(
    *,
    persisted_report: DiagnosisReport | None,
    approval: ApprovalRequest,
    session_id: str,
) -> DiagnosisReport:
    """Build an approval-resumed report when the in-memory checkpoint is gone."""
    if persisted_report is None:
        raise LookupError(f"No persisted report for incident {approval.incident_id}")

    approval_payload = approval.model_dump(mode="json")
    risk_summary = dict(persisted_report.risk_summary or {})
    risk_summary["approval_decision"] = approval_payload
    approval_decision = dict(persisted_report.approval_decision or {})
    approval_decision.update(
        {
            "approval_id": approval.approval_id,
            "action": approval.action,
            "risk_level": approval.risk_level,
            "status": approval.status,
            "reason": approval.reason,
            "tool_name": approval.tool_name,
            "requested_by": approval.requested_by,
            "created_at": approval.created_at.isoformat(),
            "decided_by": approval.decided_by,
            "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
            "decision_reason": approval.decision_reason,
        }
    )
    uncertainties = [
        item
        for item in persisted_report.uncertainties
        if "等待人工审批" not in item and "需要人工审批" not in item
    ]
    uncertainties.append(
        "审批已通过；本次恢复使用持久化报告补齐 Trace 和报告闭环，后续风险操作需进入安全变更流程。"
    )
    summary = persisted_report.summary
    if "审批已通过" not in summary:
        summary = f"{summary} 审批已通过，已基于持久化报告补齐恢复闭环。"

    markdown = _append_resume_markdown(
        persisted_report.markdown,
        approval=approval,
        session_id=session_id,
    )
    return persisted_report.model_copy(
        update={
            "status": "approval_resumed",
            "approval_status": "approved",
            "approval_decision": approval_decision,
            "risk_summary": risk_summary,
            "manual_action_required": True,
            "summary": summary,
            "uncertainties": list(dict.fromkeys(uncertainties))[:8],
            "markdown": markdown,
        }
    )


def _append_resume_markdown(
    markdown: str,
    *,
    approval: ApprovalRequest,
    session_id: str,
) -> str:
    """Append a stable resume audit section to an existing report markdown."""
    base = markdown.strip() or f"# {approval.incident_id} AIOps 诊断报告"
    section = "\n".join(
        [
            "",
            "## 审批恢复记录",
            f"- 审批ID：{approval.approval_id}",
            f"- 审批状态：{approval.status}",
            f"- 审批人：{approval.decided_by or '未记录'}",
            f"- 审批原因：{approval.decision_reason or approval.reason or '未填写'}",
            f"- 恢复 session：{session_id}",
            "- 恢复边界：使用持久化报告补齐 Trace 和报告闭环；"
            "Agent 不直接执行生产写操作，后续风险操作需进入安全变更流程。",
        ]
    )
    if "## 审批恢复记录" in base:
        return base
    return f"{base}\n{section}"
