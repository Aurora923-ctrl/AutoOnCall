"""High-level stage cards for incident replay."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.models.approval import ApprovalRequest
from app.services.aiops_read_models.common import _as_mapping
from app.services.aiops_read_models.replay_flow import (
    replay_approval_stage_status,
    replay_approval_stage_summary,
)
from app.services.aiops_read_models.replay_timeline import latest_timeline_by_stage


def build_replay_stages(
    *,
    overview: dict[str, Any],
    timeline: list[dict[str, Any]],
    report_payload: dict[str, Any],
    approvals: list[ApprovalRequest],
    change_executions: list[dict[str, Any]],
    evaluation: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the high-level replay stage cards."""
    events_by_stage: Counter[str] = Counter(str(item.get("stage") or "") for item in timeline)
    failed_by_stage: Counter[str] = Counter(
        str(item.get("stage") or "")
        for item in timeline
        if str(item.get("status") or "") in {"failed", "error", "blocked"}
    )
    latest_by_stage = latest_timeline_by_stage(timeline)
    diagnosis_chain = _as_mapping(overview.get("diagnosis_chain"))
    report_exists = bool(report_payload)
    latest_change = change_executions[-1] if change_executions else {}
    evaluation_payload = evaluation or {}

    return [
        replay_stage_card(
            "alert",
            "告警进入",
            "completed" if overview.get("summary") or overview.get("title") else "pending",
            str(overview.get("summary") or overview.get("title") or ""),
            events_by_stage,
            failed_by_stage,
            latest_by_stage,
        ),
        replay_stage_card(
            "planner",
            "Planner 生成计划",
            "completed" if diagnosis_chain.get("plan") or events_by_stage["planner"] else "pending",
            _stage_summary_from_latest(latest_by_stage.get("planner"), "已生成诊断计划"),
            events_by_stage,
            failed_by_stage,
            latest_by_stage,
        ),
        replay_stage_card(
            "executor",
            "Executor 工具取证",
            "completed"
            if diagnosis_chain.get("tool_calls") or events_by_stage["executor"]
            else "pending",
            _stage_summary_from_latest(latest_by_stage.get("executor"), "已执行工具取证"),
            events_by_stage,
            failed_by_stage,
            latest_by_stage,
        ),
        replay_stage_card(
            "replanner",
            "Replanner 调整",
            "completed" if events_by_stage["replanner"] else "not_observed",
            _stage_summary_from_latest(latest_by_stage.get("replanner"), "未观察到重规划事件"),
            events_by_stage,
            failed_by_stage,
            latest_by_stage,
        ),
        replay_stage_card(
            "approval",
            "审批与风险控制",
            replay_approval_stage_status(approvals),
            replay_approval_stage_summary(approvals),
            events_by_stage,
            failed_by_stage,
            latest_by_stage,
        ),
        replay_stage_card(
            "change",
            "安全变更",
            str(latest_change.get("lifecycle_status") or latest_change.get("status") or "not_started"),
            str(latest_change.get("status") or "未启动安全变更流程"),
            events_by_stage,
            failed_by_stage,
            latest_by_stage,
        ),
        replay_stage_card(
            "report",
            "最终报告",
            "completed" if report_exists else "pending",
            str(report_payload.get("root_cause") or report_payload.get("summary") or "报告未生成"),
            events_by_stage,
            failed_by_stage,
            latest_by_stage,
        ),
        replay_stage_card(
            "evaluation",
            "评测结果",
            str(evaluation_payload.get("status") or "not_linked"),
            str(
                evaluation_payload.get("summary")
                or evaluation_payload.get("message")
                or "单次评测暂未绑定"
            ),
            events_by_stage,
            failed_by_stage,
            latest_by_stage,
        ),
    ]


def replay_stage_card(
    key: str,
    label: str,
    status: str,
    summary: str,
    events_by_stage: Counter[str],
    failed_by_stage: Counter[str],
    latest_by_stage: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return one normalized replay stage card."""
    latest = latest_by_stage.get(key) or {}
    return {
        "key": key,
        "label": label,
        "status": status,
        "summary": summary,
        "event_count": events_by_stage.get(key, 0),
        "failed_event_count": failed_by_stage.get(key, 0),
        "latest_event": latest,
        "updated_at": str(latest.get("created_at") or ""),
    }


def _stage_summary_from_latest(item: dict[str, Any] | None, fallback: str) -> str:
    if not item:
        return fallback
    return str(item.get("summary") or item.get("output_summary") or fallback)
