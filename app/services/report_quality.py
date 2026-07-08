"""Evidence quality and confidence helpers for diagnosis reports."""

from __future__ import annotations

from typing import Any

from app.services.evidence_quality import (
    build_evidence_quality_profile,
    source_quality_confidence_cap,
)
from app.utils.structured_data import as_dict as _as_dict, dedupe_strings as _dedupe_strings

STATUS_CONFIDENCE_CAPS = {
    "incomplete": 0.55,
    "degraded": 0.74,
    "needs_human": 0.62,
}


def build_evidence_profile(
    evidence: list[dict[str, Any]],
    evidence_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Return evidence distribution metadata for report quality sections."""
    profile = _as_dict(evidence_analysis.get("evidence_profile"))
    if profile:
        return profile
    return build_evidence_quality_profile(evidence)


def build_confidence_reason(
    evidence: list[dict[str, Any]],
    evidence_analysis: dict[str, Any],
    errors: list[str],
) -> str:
    """Build a concise explanation for the final report confidence."""
    reasons = [
        str(item) for item in evidence_analysis.get("confidence_reasons", []) if str(item).strip()
    ]
    if not reasons:
        reasons = [
            f"{item.get('source_tool', 'unknown')}: {item.get('confidence_reason')}"
            for item in evidence
            if item.get("confidence_reason")
        ]
    if errors:
        reasons.append(f"{len(errors)} 个错误降低报告置信度")
    return "；".join(_dedupe_strings(reasons[:8])) or "基于证据数量、工具状态和风险状态综合计算"


def build_uncertainties(
    evidence_analysis: dict[str, Any],
    errors: list[str],
    warnings: list[str],
    risk_summary: dict[str, Any],
    status: str,
) -> list[str]:
    """Build the uncertainty list shown in reports and incident views."""
    uncertainties = [
        str(item) for item in evidence_analysis.get("conflicts", []) if str(item).strip()
    ]
    uncertainties.extend(
        f"缺失关键证据: {item}"
        for item in evidence_analysis.get("missing_evidence", [])
        if str(item).strip()
    )
    uncertainties.extend(warnings)
    uncertainties.extend(errors)
    if risk_summary.get("policy") == "forbidden":
        uncertainties.append("存在禁止自动执行动作，处置必须转人工变更流程")
    elif risk_summary.get("need_approval") or status == "waiting_approval":
        uncertainties.append("后续变更动作需要人工审批，Agent 仅输出建议不自动执行")
    return _dedupe_strings(uncertainties)[:8]


def calculate_confidence(
    evidence: list[dict[str, Any]],
    errors: list[str],
    manual_action_required: bool,
    evidence_analysis: dict[str, Any] | None = None,
) -> float:
    """Calculate bounded report confidence from evidence and analysis metadata."""
    if not evidence:
        base = 0.25
    else:
        scoring_evidence = [
            item
            for item in evidence
            if str(item.get("evidence_type") or "") not in {"runbook", "risk"}
        ] or evidence
        values = []
        for item in scoring_evidence:
            confidence = item.get("confidence")
            if isinstance(confidence, int | float):
                values.append(float(confidence))
        base = sum(values) / len(values) if values else 0.5

    analysis = _as_dict(evidence_analysis)
    analysis_confidence = analysis.get("confidence")
    analysis_confidence_value: float | None = None
    if isinstance(analysis_confidence, int | float):
        analysis_confidence_value = float(analysis_confidence)

    top_hypothesis_confidence = _top_hypothesis_confidence(analysis)
    analysis_confidence_candidates = [
        value
        for value in (analysis_confidence_value, top_hypothesis_confidence)
        if value is not None and value > 0
    ]
    if analysis_confidence_candidates:
        base = max(base, *analysis_confidence_candidates)
    if errors and not analysis_confidence_candidates:
        base -= min(0.18, 0.03 * len(errors))
    if (
        errors or _has_failed_diagnostic_evidence(evidence)
    ) and _has_enough_successful_diagnostic_evidence(evidence):
        base = max(base, 0.55)

    source_quality_cap = source_quality_confidence_cap(evidence, analysis)
    if source_quality_cap is not None:
        base = min(base, source_quality_cap)
    sufficiency = _as_dict(_as_dict(analysis.get("evidence_profile")).get("sufficiency"))
    cap = sufficiency.get("confidence_cap")
    if isinstance(cap, int | float):
        base = min(base, float(cap))
    report_status = str(analysis.get("report_status") or "")
    if report_status in STATUS_CONFIDENCE_CAPS:
        base = min(base, STATUS_CONFIDENCE_CAPS[report_status])

    return round(max(0.0, min(1.0, base)), 2)


def _top_hypothesis_confidence(evidence_analysis: dict[str, Any]) -> float | None:
    ranking = evidence_analysis.get("hypothesis_ranking")
    if not isinstance(ranking, list) or not ranking:
        return None
    top = ranking[0] if isinstance(ranking[0], dict) else {}
    confidence = top.get("confidence")
    return float(confidence) if isinstance(confidence, int | float) else None


def _has_failed_diagnostic_evidence(evidence: list[dict[str, Any]]) -> bool:
    """Return True when tool failure is captured as structured evidence."""
    for item in evidence:
        raw_data = _as_dict(item.get("raw_data"))
        if raw_data.get("status") == "failed":
            return True
    return False


def _has_enough_successful_diagnostic_evidence(evidence: list[dict[str, Any]]) -> bool:
    """Return True for graceful-degradation reports with enough supporting evidence."""
    successful = 0
    for item in evidence:
        evidence_type = str(item.get("evidence_type") or "")
        if evidence_type == "risk":
            continue
        raw_data = _as_dict(item.get("raw_data"))
        if raw_data.get("status") == "success" or (
            evidence_type == "runbook" and item.get("stance") == "supporting"
        ):
            successful += 1
    return successful >= 3


