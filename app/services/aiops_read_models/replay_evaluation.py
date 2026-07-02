"""Evaluation matching and quality checks for incident replay."""

from __future__ import annotations

import re
from typing import Any

from app.services.aiops_read_models.common import (
    _as_bool,
    _as_list,
    _as_mapping,
    _safe_float,
)
from app.services.aiops_read_models.replay_constants import DEMO_INCIDENT_EVAL_CASE_IDS


def build_replay_evaluation(
    *,
    incident_id: str,
    overview: dict[str, Any],
    report_payload: dict[str, Any],
    metrics: dict[str, Any],
    evidence_quality: dict[str, Any],
    tooling: dict[str, Any],
    replanner_decisions: list[dict[str, Any]],
    evaluation_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the single-incident evaluation view for diagnosis replay."""
    matched_case = match_replay_eval_case(
        incident_id=incident_id,
        overview=overview,
        report_payload=report_payload,
        evaluation_summary=evaluation_summary,
    )
    if matched_case:
        return build_linked_replay_evaluation(matched_case, metrics=metrics, tooling=tooling)
    return build_heuristic_replay_evaluation(
        evaluation_summary=evaluation_summary,
        metrics=metrics,
        report_payload=report_payload,
        evidence_quality=evidence_quality,
        tooling=tooling,
        replanner_decisions=replanner_decisions,
    )


def match_replay_eval_case(
    *,
    incident_id: str,
    overview: dict[str, Any],
    report_payload: dict[str, Any],
    evaluation_summary: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Find the offline evaluation case that best matches one replay incident."""
    cases = _as_list((evaluation_summary or {}).get("cases"))
    if not cases:
        return None

    candidate_ids = replay_eval_case_candidate_ids(incident_id, overview, report_payload)
    for item in cases:
        case = _as_mapping(item)
        if candidate_ids.intersection(replay_eval_case_identifiers(case)):
            return case

    best_case: dict[str, Any] | None = None
    best_score = 0
    target_text = normalize_match_text(
        " ".join(
            [
                incident_id,
                str(overview.get("title") or ""),
                str(overview.get("service_name") or ""),
                str(overview.get("summary") or ""),
                str(overview.get("root_cause") or report_payload.get("root_cause") or ""),
            ]
        )
    )
    for item in cases:
        case = _as_mapping(item)
        case_text = normalize_match_text(
            " ".join(
                [
                    str(case.get("id") or ""),
                    str(case.get("title") or ""),
                    str(_as_mapping(case.get("incident")).get("service_name") or ""),
                ]
            )
        )
        score = replay_eval_case_match_score(target_text, case_text)
        if score > best_score:
            best_case = case
            best_score = score
    return best_case if best_score >= 3 else None


def replay_eval_case_candidate_ids(
    incident_id: str,
    overview: dict[str, Any],
    report_payload: dict[str, Any],
) -> set[str]:
    """Return normalized case ids that can represent one replay incident."""
    values = {
        incident_id,
        str(overview.get("incident_id") or ""),
        str(report_payload.get("incident_id") or ""),
        str(_as_mapping(overview.get("lifecycle")).get("case_id") or ""),
    }
    normalized_incident_id = normalize_eval_identifier(incident_id)
    mapped_case_id = DEMO_INCIDENT_EVAL_CASE_IDS.get(normalized_incident_id)
    if mapped_case_id:
        values.add(mapped_case_id)
    return {normalize_eval_identifier(value) for value in values if value}


def replay_eval_case_identifiers(case: dict[str, Any]) -> set[str]:
    """Return normalized identifiers from an eval result or original eval case."""
    incident = _as_mapping(case.get("incident"))
    values = {
        str(case.get("id") or ""),
        str(case.get("case_id") or ""),
        str(case.get("incident_id") or ""),
        str(incident.get("incident_id") or ""),
    }
    return {normalize_eval_identifier(value) for value in values if value}


def replay_eval_case_match_score(target_text: str, case_text: str) -> int:
    """Score a fuzzy text match without pretending it is ground truth."""
    if not target_text or not case_text:
        return 0
    target_tokens = {token for token in target_text.split() if len(token) >= 3}
    case_tokens = {token for token in case_text.split() if len(token) >= 3}
    overlap = target_tokens.intersection(case_tokens)
    return len(overlap)


def build_linked_replay_evaluation(
    case: dict[str, Any],
    *,
    metrics: dict[str, Any],
    tooling: dict[str, Any],
) -> dict[str, Any]:
    """Build a replay evaluation view from a matched offline eval case."""
    case_metrics = _as_mapping(case.get("metrics"))
    case_id = str(case.get("id") or case.get("case_id") or "")
    passed = bool(case.get("passed", False))
    unnecessary_tool_rate_value = _safe_float(case.get("unnecessary_tool_rate"))
    latency_ms = _safe_float(case.get("latency_ms"))
    evidence_sufficient = combined_boolean_metric(
        case_metrics,
        ["evidence_count_hit", "confidence_hit", "report_contains_evidence"],
    )
    metric_items = [
        replay_evaluation_metric(
            "root_cause_hit",
            "根因命中",
            case_metrics.get("root_cause_hit"),
            "boolean",
            "报告根因是否覆盖评测用例期望关键词。",
            "cases[].metrics.root_cause_hit",
        ),
        replay_evaluation_metric(
            "tool_hit",
            "工具选择命中",
            case_metrics.get("tool_hit"),
            "boolean",
            "Planner 是否选择了期望诊断工具。",
            "cases[].metrics.tool_hit",
        ),
        replay_evaluation_metric(
            "executed_tool_hit",
            "执行工具命中",
            case_metrics.get("executed_tool_hit"),
            "boolean",
            "Executor 是否真正执行关键诊断工具。",
            "cases[].metrics.executed_tool_hit",
        ),
        replay_evaluation_metric(
            "evidence_sufficient",
            "证据充分",
            evidence_sufficient,
            "boolean",
            "综合证据数量、报告证据引用和置信度判断。",
            "cases[].metrics.evidence_count_hit/confidence_hit/report_contains_evidence",
        ),
        replay_evaluation_metric(
            "tool_redundancy",
            "工具冗余",
            unnecessary_tool_rate_value,
            "percent",
            "非期望诊断工具占比；越低越好。",
            "cases[].unnecessary_tool_rate",
            status=boolean_metric_status(case_metrics.get("unnecessary_tool_rate")),
        ),
        replay_evaluation_metric(
            "trace_completeness",
            "Trace 完整",
            case_metrics.get("trace_completeness"),
            "boolean",
            "诊断是否留下 trace_id、工具调用、证据和报告闭环。",
            "cases[].metrics.trace_completeness",
        ),
        replay_evaluation_metric(
            "latency_ms",
            "评测耗时",
            latency_ms,
            "duration_ms",
            "该离线 case 的端到端执行耗时。",
            "cases[].latency_ms",
            status="observed" if latency_ms is not None else "unknown",
        ),
    ]
    return {
        "status": "passed" if passed else "failed",
        "linked": True,
        "source": "offline_eval_summary",
        "case_id": case_id,
        "passed": passed,
        "summary": f"已匹配离线评测用例 {case_id or 'unknown'}，结果：{'通过' if passed else '未通过'}。",
        "message": "该结果来自最近一次离线评测摘要，可用于面试展示单条故障的诊断质量。",
        "metrics": metric_items,
        "failed_metrics": _as_list(case.get("failed_metrics")),
        "failure_reasons": _as_mapping(case.get("failure_reasons")),
        "tool_snapshot": {
            "planned_tools": _as_list(case.get("planned_tools")),
            "executed_tools": _as_list(case.get("executed_tools")),
            "failed_tools": _as_list(case.get("failed_tools")),
            "duplicate_tool_candidates": _as_list(tooling.get("duplicate_tool_candidates")),
            "replay_tool_call_count": metrics.get("tool_call_count", 0),
        },
    }


def build_heuristic_replay_evaluation(
    *,
    evaluation_summary: dict[str, Any] | None,
    metrics: dict[str, Any],
    report_payload: dict[str, Any],
    evidence_quality: dict[str, Any],
    tooling: dict[str, Any],
    replanner_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a transparent quality check when no offline eval case is linked."""
    has_eval_summary = bool((evaluation_summary or {}).get("available"))
    duplicate_tools = _as_list(tooling.get("duplicate_tool_candidates"))
    evidence_sufficient = replay_evidence_is_sufficient(
        metrics=metrics,
        evidence_quality=evidence_quality,
        replanner_decisions=replanner_decisions,
    )
    trace_complete = bool(
        metrics.get("trace_event_count")
        and metrics.get("tool_call_count")
        and metrics.get("evidence_count")
        and report_payload
    )
    metric_items = [
        replay_evaluation_metric(
            "root_cause_hit",
            "根因命中",
            None,
            "unknown",
            "未匹配离线 case，缺少期望根因关键词，不能计算命中。",
            "replay_quality.no_expected_case",
        ),
        replay_evaluation_metric(
            "tool_hit",
            "工具选择命中",
            None,
            "unknown",
            "未匹配离线 case，缺少期望工具集合，不能计算命中。",
            "replay_quality.no_expected_case",
        ),
        replay_evaluation_metric(
            "evidence_sufficient",
            "证据充分",
            evidence_sufficient,
            "boolean",
            "基于证据数量、平均置信度、报告置信度和 Replanner 判断的自动检查。",
            "replay_quality.evidence",
        ),
        replay_evaluation_metric(
            "tool_redundancy",
            "工具冗余",
            len(duplicate_tools),
            "integer",
            "当前 Replay 中重复调用的工具数量。",
            "replay_quality.duplicate_tool_candidates",
            status="passed" if not duplicate_tools else "warning",
        ),
        replay_evaluation_metric(
            "trace_completeness",
            "Trace 完整",
            trace_complete,
            "boolean",
            "当前 Replay 是否具备事件、工具、证据和报告闭环。",
            "replay_quality.trace_completeness",
        ),
        replay_evaluation_metric(
            "latency_ms",
            "回放耗时",
            metrics.get("total_latency_ms"),
            "duration_ms",
            "当前 Trace 记录的工具与节点耗时总和。",
            "replay.metrics.total_latency_ms",
            status="observed" if metrics.get("total_latency_ms") else "unknown",
        ),
    ]
    return {
        "status": "heuristic",
        "linked": False,
        "source": "replay_quality",
        "case_id": "",
        "passed": None,
        "summary": (
            "未匹配到当前 Incident 的离线评测 case，展示 Replay 自动质量检查。"
            if has_eval_summary
            else "未找到离线评测摘要，展示 Replay 自动质量检查。"
        ),
        "message": "自动检查不会伪造根因或工具命中率，只展示当前诊断链路本身是否可追踪、可解释。",
        "metrics": metric_items,
        "failed_metrics": [
            item["key"] for item in metric_items if item.get("status") in {"failed", "warning"}
        ],
        "failure_reasons": {},
        "tool_snapshot": {
            "planned_tools": [],
            "executed_tools": list(_as_mapping(tooling.get("by_tool")).keys()),
            "failed_tools": [
                str(item.get("tool_name") or "unknown")
                for item in _as_list(tooling.get("items"))
                if isinstance(item, dict)
                and str(item.get("status") or "") in {"failed", "error", "blocked"}
            ],
            "duplicate_tool_candidates": duplicate_tools,
            "replay_tool_call_count": metrics.get("tool_call_count", 0),
        },
    }


def replay_evaluation_metric(
    key: str,
    label: str,
    value: Any,
    value_type: str,
    description: str,
    source: str,
    *,
    status: str | None = None,
) -> dict[str, Any]:
    """Return a normalized single metric item for the replay evaluation panel."""
    return {
        "key": key,
        "label": label,
        "value": value,
        "value_type": value_type,
        "status": status or boolean_metric_status(value),
        "description": description,
        "source": source,
    }


def combined_boolean_metric(case_metrics: dict[str, Any], keys: list[str]) -> bool | None:
    """Combine multiple boolean eval metrics while preserving unknown."""
    values = [_as_bool(case_metrics.get(key)) for key in keys if key in case_metrics]
    if not values:
        return None
    return all(values)


def replay_evidence_is_sufficient(
    *,
    metrics: dict[str, Any],
    evidence_quality: dict[str, Any],
    replanner_decisions: list[dict[str, Any]],
) -> bool:
    """Heuristically judge whether replay evidence is enough to explain the diagnosis."""
    evidence_count = int(metrics.get("evidence_count") or 0)
    report_confidence = _safe_float(metrics.get("confidence")) or 0.0
    average_evidence_confidence = _safe_float(evidence_quality.get("average_confidence")) or 0.0
    has_replanner_sufficient = any(
        bool(item.get("evidence_sufficient")) for item in replanner_decisions
    )
    return bool(
        evidence_count >= 2
        and not evidence_quality.get("has_not_configured")
        and (average_evidence_confidence >= 0.6 or report_confidence >= 0.6 or has_replanner_sufficient)
    )


def boolean_metric_status(value: Any) -> str:
    """Convert a metric value into the small status vocabulary used by the UI."""
    parsed = _as_bool(value)
    if parsed is True:
        return "passed"
    if parsed is False:
        return "failed"
    return "unknown"


def normalize_eval_identifier(value: str) -> str:
    """Normalize incident/case ids for deterministic matching."""
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def normalize_match_text(value: str) -> str:
    """Normalize free text enough for conservative fallback matching."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", str(value or "").strip().lower())
