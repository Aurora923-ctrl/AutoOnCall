"""Lifecycle transitions for persisted diagnosis reports."""

from __future__ import annotations

from typing import Any

from app.models.incident import utc_now
from app.models.report import DiagnosisReport
from app.services.change_execution_read_models import (
    change_execution_next_steps,
    change_execution_uncertainties,
)
from app.services.change_plan_builder import update_change_plan_status
from app.services.incident_lifecycle import (
    manual_action_required_from_change_execution,
    status_from_change_execution,
)
from app.services.report_markdown import render_markdown
from app.utils.structured_data import dedupe_strings


class ReportLifecycle:
    """Apply approval and safe-change state transitions to reports."""

    def apply_approval_decision(
        self,
        report: DiagnosisReport,
        *,
        approval_status: str,
        decided_by: str | None = None,
        reason: str = "",
        approval_request: dict[str, Any] | None = None,
    ) -> DiagnosisReport:
        if approval_status not in {"approved", "rejected", "cancelled"}:
            return report

        status = f"approval_{approval_status}"
        decision_text = {
            "approved": "审批已通过",
            "rejected": "审批已拒绝",
            "cancelled": "审批已取消",
        }[approval_status]
        actor_text = f"，处理人：{decided_by}" if decided_by else ""
        reason_text = f"，原因：{reason}" if reason else ""
        risk_summary = dict(report.risk_summary or {})
        decision_snapshot = build_approval_decision(
            approval_request or report.approval_decision,
            risk_summary,
        )
        decision_snapshot.update(
            {
                "status": approval_status,
                "decided_by": decided_by,
                "decision_reason": reason,
                "decided_at": decision_snapshot.get("decided_at") or utc_now().isoformat(),
            }
        )
        risk_summary["approval_decision"] = {
            **decision_snapshot,
            "reason": decision_snapshot.get("reason") or reason,
        }
        uncertainties = [
            item
            for item in report.uncertainties
            if "等待人工审批" not in item and "需要人工审批" not in item
        ]
        uncertainties.append(
            f"{decision_text}{actor_text}{reason_text}；Agent 不直接执行生产写操作，"
            "审批通过后需进入安全变更流程。"
        )
        summary = report.summary
        if "审批已" not in summary:
            follow_up = (
                "审批通过后进入 pre-check、dry-run、sandbox 或人工执行记录。"
                if approval_status == "approved"
                else "该审批不再授权后续诊断恢复或安全变更执行。"
            )
            summary = f"{summary} {decision_text}；{follow_up}"

        return _render_updated_report(
            report,
            update={
                "status": status,
                "approval_status": approval_status,
                "approval_decision": decision_snapshot,
                "risk_summary": risk_summary,
                "change_plan": update_change_plan_status(
                    dict(report.change_plan or {}),
                    approval_status,
                ),
                "manual_action_required": True,
                "summary": summary,
                "uncertainties": dedupe_strings(uncertainties)[:8],
            },
        )

    def apply_change_execution(
        self,
        report: DiagnosisReport,
        *,
        execution: dict[str, Any],
    ) -> DiagnosisReport:
        status = str(execution.get("status") or "")
        risk_summary = dict(report.risk_summary or {})
        risk_summary["change_execution"] = execution
        return _render_updated_report(
            report,
            update={
                "status": status_from_change_execution(status),
                "change_executions": _upsert_change_execution_snapshot(
                    report.change_executions,
                    execution,
                ),
                "risk_summary": risk_summary,
                "approval_status": (
                    "approved" if execution.get("approval_id") else report.approval_status
                ),
                "approval_decision": _change_execution_approval_decision(
                    report.approval_decision,
                    execution,
                ),
                "manual_action_required": manual_action_required_from_change_execution(
                    status,
                    fallback=report.manual_action_required,
                ),
                "summary": _append_change_execution_summary(report.summary, status),
                "next_steps": change_execution_next_steps(report.next_steps, status),
                "uncertainties": change_execution_uncertainties(
                    report.uncertainties,
                    status,
                ),
            },
        )


def build_approval_decision(
    pending_approval: dict[str, Any],
    risk_summary: dict[str, Any],
) -> dict[str, Any]:
    """Return a stable approval lifecycle snapshot for reports and UI."""
    source = dict(pending_approval or {})
    risk = dict(risk_summary or {})
    if not source and not risk.get("need_approval") and risk.get("policy") != "forbidden":
        return {}

    return {
        "approval_id": source.get("approval_id", ""),
        "action": source.get("action") or risk.get("action") or "",
        "risk_level": source.get("risk_level") or risk.get("risk_level") or "low",
        "status": source.get("status")
        or ("forbidden" if risk.get("policy") == "forbidden" else "required"),
        "reason": source.get("reason") or risk.get("reason") or "",
        "tool_name": source.get("tool_name"),
        "requested_by": source.get("requested_by", "aiops-agent"),
        "created_at": source.get("created_at"),
        "decided_by": source.get("decided_by"),
        "decided_at": source.get("decided_at"),
        "decision_reason": source.get("decision_reason") or source.get("reason") or "",
    }


def _upsert_change_execution_snapshot(
    existing: list[dict[str, Any]],
    execution: dict[str, Any],
) -> list[dict[str, Any]]:
    execution_id = str(execution.get("change_execution_id") or "")
    snapshots = [dict(item) for item in existing if isinstance(item, dict)]
    if not execution_id:
        return (snapshots + [execution])[-10:]
    for index, item in enumerate(snapshots):
        if str(item.get("change_execution_id") or "") == execution_id:
            snapshots[index] = execution
            break
    else:
        snapshots.append(execution)
    return snapshots[-10:]


def _append_change_execution_summary(summary: str, status: str) -> str:
    status_text = status_from_change_execution(status)
    if not status_text:
        return summary
    marker = "安全变更流程当前状态："
    change_summary = f"{marker}{status_text}。"
    if marker in summary:
        return summary.split(marker, 1)[0].rstrip() + " " + change_summary
    return f"{summary.rstrip()} {change_summary}".strip()


def _change_execution_approval_decision(
    existing: dict[str, Any],
    execution: dict[str, Any],
) -> dict[str, Any]:
    decision = dict(existing or {})
    approval_id = str(execution.get("approval_id") or "")
    if approval_id:
        decision["approval_id"] = approval_id
        decision["status"] = "approved"
    return decision


def _render_updated_report(
    report: DiagnosisReport,
    *,
    update: dict[str, Any],
) -> DiagnosisReport:
    updated = report.model_copy(update=update)
    return updated.model_copy(update={"markdown": render_markdown(updated)})
