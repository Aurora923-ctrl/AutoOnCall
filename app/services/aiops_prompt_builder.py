"""Prompt rendering helpers for AIOps diagnosis requests."""

from __future__ import annotations

import json
from typing import Any

from app.models.incident import Incident
from app.services.context_budget import DEFAULT_CONTEXT_BUDGETER, ContextBudgeter

RAW_ALERT_PRIORITY_KEYS = (
    "requested_action",
    "action",
    "sql",
    "audited",
    "reason",
    "description",
    "alertname",
    "dependency",
)


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
    ordered_alert = {key: raw_alert[key] for key in RAW_ALERT_PRIORITY_KEYS if key in raw_alert}
    ordered_alert.update(
        (key, raw_alert[key]) for key in sorted(raw_alert) if key not in ordered_alert
    )
    return _format_priority_json(
        ordered_alert,
        priority_keys=RAW_ALERT_PRIORITY_KEYS,
        limit=active_budgeter.limit(limit),
        budgeter=active_budgeter,
    )


def _format_priority_json(
    values: dict[str, Any],
    *,
    priority_keys: tuple[str, ...],
    limit: int,
    budgeter: ContextBudgeter,
) -> str:
    """Keep every present priority field before spending budget on optional fields."""
    full_text = _json_text(values, budgeter)
    if len(full_text) <= limit:
        return full_text

    marker = budgeter.budget.truncation_marker
    content_limit = max(0, limit - len(marker))
    priority = {key: values[key] for key in priority_keys if key in values}
    optional = [(key, value) for key, value in values.items() if key not in priority]
    rendered_priority = _fit_priority_fields(priority, content_limit, budgeter)
    selected = dict(rendered_priority)

    for key, value in optional:
        candidate = {**selected, key: value}
        if len(_json_text(candidate, budgeter)) > content_limit:
            break
        selected = candidate

    rendered = _json_text(selected, budgeter)
    return f"{rendered}{marker}"[:limit]


def _fit_priority_fields(
    values: dict[str, Any],
    limit: int,
    budgeter: ContextBudgeter,
) -> dict[str, Any]:
    """Bound large priority values while retaining their keys and scalar types."""
    if not values or limit <= 0:
        return {}
    if len(_json_text(values, budgeter)) <= limit:
        return values

    string_keys = [key for key, value in values.items() if isinstance(value, str)]
    if not string_keys:
        return values

    low = 0
    high = max(len(str(values[key])) for key in string_keys)
    best = {**values, **dict.fromkeys(string_keys, "")}
    while low <= high:
        per_field_limit = (low + high) // 2
        candidate = dict(values)
        for key in string_keys:
            candidate[key] = budgeter.text(values[key], limit=per_field_limit)
        if len(_json_text(candidate, budgeter)) > limit:
            high = per_field_limit - 1
        else:
            best = candidate
            low = per_field_limit + 1
    return best


def _json_text(value: Any, budgeter: ContextBudgeter) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            indent=budgeter.budget.json_indent,
        )
    except TypeError:
        return str(value)
