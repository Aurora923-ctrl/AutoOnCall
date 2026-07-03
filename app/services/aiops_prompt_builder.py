"""Prompt rendering helpers for AIOps diagnosis requests."""

from __future__ import annotations

import json
from typing import Any

from app.models.incident import Incident


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


def format_raw_alert_for_prompt(raw_alert: dict[str, Any], max_chars: int = 4000) -> str:
    """Serialize raw alert fields for planning while keeping the prompt bounded."""
    if not raw_alert:
        return ""
    text = json.dumps(raw_alert, ensure_ascii=False, default=str, indent=2, sort_keys=True)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n...<truncated>"
