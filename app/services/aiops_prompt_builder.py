"""Prompt rendering helpers for AIOps diagnosis requests."""

from __future__ import annotations

from typing import Any

from app.models.incident import Incident
from app.services.context_budget import DEFAULT_CONTEXT_BUDGETER, ContextBudgeter


def build_incident_diagnosis_input(base_task: str, incident: Incident | None) -> str:
    """Render a structured incident into the planner-facing diagnosis request."""
    if incident is None:
        return base_task

    lines = [
        "请基于以下结构化故障事件进行诊断，不要只按通用巡检处理。",
        "",
        "## 故障事件",
        f"- incident_id: {incident.incident_id}",
        f"- title: {incident.title}",
        f"- service_name: {incident.service_name}",
        f"- severity: {incident.severity}",
        f"- environment: {incident.environment}",
        f"- status: {incident.status}",
        f"- start_time: {incident.start_time.isoformat()}",
        f"- symptom: {incident.symptom or '未提供'}",
    ]

    raw_alert_text = format_raw_alert_for_prompt(incident.raw_alert)
    if raw_alert_text:
        lines.extend(
            [
                "",
                "## 原始告警",
                "```json",
                raw_alert_text,
                "```",
            ]
        )

    lines.extend(
        [
            "",
            "## 诊断与报告要求",
            base_task.strip(),
        ]
    )
    return "\n".join(lines)


def format_raw_alert_for_prompt(
    raw_alert: dict[str, Any],
    max_chars: int | None = None,
    budgeter: ContextBudgeter | None = None,
) -> str:
    """Serialize raw alert fields for planning while keeping the prompt bounded."""
    if not raw_alert:
        return ""
    active_budgeter = budgeter or DEFAULT_CONTEXT_BUDGETER
    limit = max_chars if max_chars is not None else active_budgeter.budget.raw_alert_chars
    return active_budgeter.json(raw_alert, limit=limit, sort_keys=True)
