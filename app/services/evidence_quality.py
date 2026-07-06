"""Shared evidence source quality rules for AIOps analysis and reports."""

from __future__ import annotations

from typing import Any

from app.services.diagnostic_signal_rules import infer_evidence_type, normalize_data_source

TRUSTED_EVIDENCE_SOURCES = {
    "prometheus",
    "loki",
    "log_gateway",
    "cmdb",
    "deploy_history",
    "redis_info",
    "kubernetes",
    "mysql",
    "ticket_api",
    "alertmanager",
    "jaeger",
    "tempo",
    "redpanda",
    "mcp_monitor",
    "mcp_cls",
}
FALLBACK_EVIDENCE_SOURCES = {
    "mock",
    "not_configured",
    "failed",
    "manual_analysis",
    "llm_toolnode_fallback",
    "rule_based",
}
DEGRADED_EVIDENCE_SOURCES = {"mcp_monitor_mixed", "unknown"}
NON_DIAGNOSTIC_EVIDENCE_TYPES = {"runbook", "risk"}

FALLBACK_ONLY_CONFIDENCE_CAP = 0.5
MIXED_WITH_FALLBACK_CONFIDENCE_CAP = 0.72


def build_evidence_quality_profile(
    evidence_items: list[Any],
    tool_records: list[Any] | None = None,
) -> dict[str, Any]:
    """Summarize evidence distribution and source quality."""
    by_type: dict[str, int] = {}
    by_stance: dict[str, int] = {}
    by_data_source: dict[str, int] = {}
    failed_tools = dedupe_strings(
        [
            str(record.get("tool_name") or "")
            for record in tool_records or []
            if isinstance(record, dict) and record.get("status") == "failed"
        ]
    )
    confidence_values: list[float] = []
    diagnostic_success_count = 0
    trusted_source_count = 0
    degraded_source_count = 0
    fallback_source_count = 0

    for evidence in evidence_items:
        if not isinstance(evidence, dict):
            continue
        evidence_type = evidence_type_for_quality(evidence)
        stance = str(evidence.get("stance") or "neutral")
        data_source = evidence_data_source(evidence)
        by_type[evidence_type] = by_type.get(evidence_type, 0) + 1
        by_stance[stance] = by_stance.get(stance, 0) + 1
        by_data_source[data_source] = by_data_source.get(data_source, 0) + 1
        confidence = evidence.get("confidence")
        if isinstance(confidence, int | float):
            confidence_values.append(float(confidence))
        raw_data = as_dict(evidence.get("raw_data"))
        if raw_data.get("status") == "failed":
            failed_tools.append(str(evidence.get("source_tool") or raw_data.get("tool_name") or ""))
        if is_successful_diagnostic_evidence(evidence, evidence_type):
            diagnostic_success_count += 1
            source_group = evidence_source_group(data_source)
            if source_group == "trusted":
                trusted_source_count += 1
            elif source_group == "fallback":
                fallback_source_count += 1
            else:
                degraded_source_count += 1

    source_quality = "trusted_or_unlabeled"
    low_quality_count = fallback_source_count + degraded_source_count
    if diagnostic_success_count and trusted_source_count == 0 and low_quality_count:
        source_quality = "fallback_only"
    elif low_quality_count:
        source_quality = "mixed_with_fallback"

    return {
        "by_type": by_type,
        "by_stance": by_stance,
        "by_data_source": by_data_source,
        "failed_tools": dedupe_strings([tool for tool in failed_tools if tool]),
        "average_evidence_confidence": average(confidence_values),
        "diagnostic_success_count": diagnostic_success_count,
        "trusted_source_count": trusted_source_count,
        "degraded_source_count": degraded_source_count,
        "fallback_source_count": fallback_source_count,
        "source_quality": source_quality,
        "supporting_count": by_stance.get("supporting", 0),
        "refuting_count": by_stance.get("refuting", 0),
        "neutral_count": by_stance.get("neutral", 0),
        "unknown_count": by_stance.get("unknown", 0),
    }


def source_quality_confidence_cap(
    evidence: list[dict[str, Any]],
    evidence_analysis: dict[str, Any],
) -> float | None:
    """Return the confidence cap implied by evidence source quality."""
    profile = as_dict(evidence_analysis.get("evidence_profile"))
    source_quality = str(profile.get("source_quality") or "")
    if source_quality == "fallback_only":
        return FALLBACK_ONLY_CONFIDENCE_CAP
    if source_quality == "mixed_with_fallback":
        return MIXED_WITH_FALLBACK_CONFIDENCE_CAP

    derived_profile = build_evidence_quality_profile(evidence)
    derived_quality = str(derived_profile.get("source_quality") or "")
    if derived_quality == "fallback_only":
        return FALLBACK_ONLY_CONFIDENCE_CAP
    if derived_quality == "mixed_with_fallback":
        return MIXED_WITH_FALLBACK_CONFIDENCE_CAP
    return None


def evidence_data_source(evidence: dict[str, Any]) -> str:
    """Return a normalized data source label for one evidence item."""
    data_source = str(evidence.get("data_source") or "").strip().lower()
    if data_source and data_source != "unknown":
        return data_source
    raw_data = as_dict(evidence.get("raw_data"))
    normalized = normalize_data_source(str(evidence.get("source_tool") or ""), raw_data)
    if normalized and normalized != "unknown":
        return normalized
    return "unknown"


def evidence_source_group(data_source: str) -> str:
    """Classify a normalized source as trusted, fallback, or degraded."""
    source = str(data_source or "").strip().lower()
    if source in TRUSTED_EVIDENCE_SOURCES:
        return "trusted"
    if source in FALLBACK_EVIDENCE_SOURCES:
        return "fallback"
    return "degraded"


def evidence_type_for_quality(evidence: dict[str, Any]) -> str:
    evidence_type = str(evidence.get("evidence_type") or "")
    if evidence_type:
        return evidence_type
    return infer_evidence_type(str(evidence.get("source_tool") or ""))


def is_successful_diagnostic_evidence(evidence: dict[str, Any], evidence_type: str) -> bool:
    if evidence_type in NON_DIAGNOSTIC_EVIDENCE_TYPES:
        return False
    return as_dict(evidence.get("raw_data")).get("status") == "success"


def evidence_data_sources(evidence_items: list[Any]) -> set[str]:
    sources: set[str] = set()
    for evidence in evidence_items:
        if isinstance(evidence, dict):
            sources.add(evidence_data_source(evidence))
    return sources


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)
