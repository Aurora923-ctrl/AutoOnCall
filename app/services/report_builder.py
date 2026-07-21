"""Report construction rules for AIOps diagnosis workflows."""

from __future__ import annotations

import json
from collections import Counter
from hashlib import sha256
from typing import Any

from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.aiops_state_utils import extract_incident_id as _extract_incident_id
from app.services.evidence_graph import build_incident_evidence_graph
from app.services.incident_lifecycle import (
    REPORT_SAFE_INSUFFICIENT_EVIDENCE_STATUSES,
    REPORT_SAFE_POLICY_BOUNDARY_STATUSES,
    report_requires_manual_action,
)
from app.services.report_lifecycle import build_approval_decision
from app.services.report_quality import (
    build_confidence_reason as _build_confidence_reason,
    build_evidence_profile as _build_evidence_profile,
    build_uncertainties as _build_uncertainties,
    calculate_confidence as _calculate_confidence,
)
from app.utils.redaction import redact_sensitive_data
from app.utils.structured_data import (
    as_dict as _as_dict,
    dedupe_strings as _dedupe_strings,
    dict_list as _dict_list,
)


class ReportBuilder:
    """Build deterministic reports without persistence side effects."""

    def build_from_state(
        self,
        state: dict[str, Any],
        *,
        trace_events: list[TraceEvent] | None = None,
        status: str = "completed",
    ) -> DiagnosisReport:
        """Build a deterministic report from the current LangGraph state."""
        incident = _as_dict(state.get("incident"))
        incident_id = _extract_incident_id(state)
        trace_id = str(state.get("trace_id") or "")
        evidence = _normalize_evidence_ids(_dict_list(state.get("gathered_evidence")))
        tool_calls = [
            _compact_tool_call(record) for record in _dict_list(state.get("tool_call_records"))
        ]
        hypotheses = [str(item) for item in state.get("hypotheses", []) if item]
        risk_summary = _as_dict(state.get("risk_assessment"))
        pending_approval = _as_dict(state.get("pending_approval"))
        evidence_analysis = _as_dict(state.get("evidence_analysis"))
        errors = [str(error) for error in state.get("errors", []) if error]
        warnings = [str(warning) for warning in state.get("warnings", []) if warning]
        evidence_profile = _build_evidence_profile(evidence, evidence_analysis)
        status, sufficiency_warnings = _apply_evidence_sufficiency_gate(
            requested_status=status,
            evidence_profile=evidence_profile,
            tool_calls=tool_calls,
            errors=errors,
            pending_approval=pending_approval,
            risk_summary=risk_summary,
        )
        degradation_analysis = _build_degradation_analysis(
            status=status,
            evidence_profile=evidence_profile,
            tool_calls=tool_calls,
            errors=errors,
        )
        evidence_analysis = {
            **evidence_analysis,
            "evidence_profile": evidence_profile,
            "report_status": status,
            "degradation_analysis": degradation_analysis,
        }
        warnings = _dedupe_strings([*warnings, *sufficiency_warnings])
        hypothesis_ranking = _build_hypothesis_ranking(hypotheses, evidence_analysis, evidence)
        selected_root_cause_id = _selected_root_cause_id(hypothesis_ranking)
        selected_root_cause = _selected_root_cause_text(hypothesis_ranking)
        has_supported_root_cause = bool(selected_root_cause_id and selected_root_cause)

        events = (
            [
                event
                for event in trace_events
                if (not trace_id or event.trace_id == trace_id) and event.incident_id == incident_id
            ]
            if trace_events is not None
            else []
        )
        approval_status = _approval_status(pending_approval, risk_summary)
        manual_action_required = _manual_action_required(
            pending_approval=pending_approval,
            risk_summary=risk_summary,
            status=status,
        )
        root_cause = selected_root_cause or _select_root_cause(errors)
        key_findings = _build_key_findings(hypotheses, evidence, tool_calls, errors)
        remediation_suggestion = _build_remediation(
            state=state,
            evidence=evidence,
            risk_summary=risk_summary,
            pending_approval=pending_approval,
        )
        conclusion_alignment = _build_conclusion_alignment(
            root_cause=root_cause,
            key_findings=key_findings,
            remediation_suggestion=remediation_suggestion,
            hypothesis_ranking=hypothesis_ranking,
            selected_root_cause_id=selected_root_cause_id,
            evidence=evidence,
            require_root_cause_alignment=has_supported_root_cause,
        )
        status, alignment_warnings = _apply_conclusion_alignment_gate(
            requested_status=status,
            alignment=conclusion_alignment,
        )
        if alignment_warnings:
            warnings = _dedupe_strings([*warnings, *alignment_warnings])
            evidence_analysis["report_status"] = status
            manual_action_required = _manual_action_required(
                pending_approval=pending_approval,
                risk_summary=risk_summary,
                status=status,
            )
            root_cause = _downgrade_unaligned_text(root_cause, conclusion_alignment, "root_cause")
            key_findings = _downgrade_unaligned_findings(key_findings, conclusion_alignment)
            remediation_suggestion = _downgrade_unaligned_text(
                remediation_suggestion,
                conclusion_alignment,
                "remediation_suggestion",
            )
        evidence_graph = build_incident_evidence_graph(
            incident_id=incident_id,
            trace_id=trace_id,
            root_cause=root_cause,
            selected_root_cause_id=selected_root_cause_id,
            hypothesis_ranking=hypothesis_ranking,
            evidence=evidence,
            tool_calls=tool_calls,
            conclusion_alignment=conclusion_alignment,
        )

        report = DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            title=_report_title(incident),
            service_name=str(incident.get("service_name") or "unknown-service"),
            severity=str(incident.get("severity") or "P2"),
            environment=str(incident.get("environment") or "unknown"),
            status=status,
            summary=_build_summary(
                incident,
                evidence,
                errors,
                status,
                root_cause=root_cause,
            ),
            root_cause=root_cause,
            hypotheses=hypotheses,
            hypothesis_ranking=hypothesis_ranking,
            selected_root_cause_id=selected_root_cause_id,
            selected_root_cause_category=_selected_root_cause_category(hypothesis_ranking),
            evidence=evidence,
            key_findings=key_findings,
            confirmed_facts=_build_confirmed_facts(evidence, tool_calls),
            inferred_conclusions=_build_inferred_conclusions(hypotheses, evidence),
            next_steps=_build_next_steps(evidence, errors, risk_summary, pending_approval),
            tool_calls=tool_calls,
            dependency_signals=_build_dependency_signals(evidence, tool_calls),
            timeline=_build_timeline(events, state.get("past_steps", [])),
            impact=_build_impact(incident, evidence, risk_summary),
            risk_summary=risk_summary,
            manual_action_required=manual_action_required,
            approval_status=approval_status,
            approval_decision=build_approval_decision(pending_approval, risk_summary),
            change_plan=_build_change_plan_snapshot(state, pending_approval, risk_summary),
            remediation_suggestion=remediation_suggestion,
            prevention=_build_prevention(evidence, errors),
            trace_summary=_build_trace_summary(events),
            errors=errors,
            warnings=warnings,
            evidence_profile=evidence_profile,
            evidence_sufficiency=_as_dict(evidence_profile.get("sufficiency")),
            degradation_analysis=degradation_analysis,
            evidence_graph=evidence_graph,
            conclusion_alignment=conclusion_alignment,
            confidence_reason=_build_confidence_reason(evidence, evidence_analysis, errors),
            uncertainties=_build_uncertainties(
                evidence_analysis,
                errors,
                warnings,
                risk_summary,
                status,
            ),
            confidence=_calculate_confidence(
                evidence,
                errors,
                manual_action_required,
                evidence_analysis,
            ),
        )
        return report


def _apply_evidence_sufficiency_gate(
    *,
    requested_status: str,
    evidence_profile: dict[str, Any],
    tool_calls: list[dict[str, Any]],
    errors: list[str],
    pending_approval: dict[str, Any],
    risk_summary: dict[str, Any],
) -> tuple[str, list[str]]:
    """Downgrade overconfident reports when the evidence set is incomplete."""
    if requested_status != "completed":
        return requested_status, []
    if risk_summary.get("policy") == "forbidden":
        return "blocked", ["报告由 completed 降级为 blocked：风险策略禁止自动执行。"]
    if pending_approval or risk_summary.get("need_approval"):
        return "waiting_approval", ["报告由 completed 降级为 waiting_approval：仍需人工审批。"]
    sufficiency = _as_dict(evidence_profile.get("sufficiency"))
    if not sufficiency:
        return requested_status, []
    missing = [str(item) for item in sufficiency.get("missing_evidence", []) if str(item).strip()]
    failed_tools = [str(item) for item in sufficiency.get("failed_tools", []) if str(item).strip()]
    if sufficiency.get("complete") and not errors and not failed_tools:
        return requested_status, []

    if errors or failed_tools:
        status = "degraded"
    elif not sufficiency.get("has_primary_domain_evidence") or not sufficiency.get(
        "has_symptom_evidence"
    ):
        status = "incomplete"
    else:
        status = "needs_human"

    details = []
    if missing:
        details.append("缺失证据：" + "、".join(missing))
    if failed_tools:
        details.append("失败工具：" + "、".join(failed_tools))
    detail_text = "；".join(details) or "证据充分性门槛未满足"
    return status, [f"报告由 completed 降级为 {status}：{detail_text}。"]


def _build_degradation_analysis(
    *,
    status: str,
    evidence_profile: dict[str, Any],
    tool_calls: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    """Explain whether a non-completed terminal report is a safe evidence boundary."""
    sufficiency = _as_dict(evidence_profile.get("sufficiency"))
    missing_evidence = [
        str(item) for item in sufficiency.get("missing_evidence", []) if str(item).strip()
    ]
    failed_tools = _dedupe_strings(
        [
            *[str(item) for item in sufficiency.get("failed_tools", []) if str(item).strip()],
            *[
                str(call.get("tool_name") or "")
                for call in tool_calls
                if str(call.get("status") or "") == "failed"
            ],
        ]
    )
    failed_tool_details = [
        {
            "tool_name": str(call.get("tool_name") or ""),
            "error_type": str(
                call.get("error_type")
                or _as_dict(call.get("execution_metadata")).get("failure_kind")
                or ""
            ),
            "error_message": str(call.get("error_message") or ""),
            "retry": _as_dict(_as_dict(call.get("execution_metadata")).get("retry")),
        }
        for call in tool_calls
        if str(call.get("status") or "") in {"failed", "error", "blocked"}
    ]
    failure_text = " ".join(
        [
            *errors,
            *[
                str(
                    call.get("error_type")
                    or _as_dict(call.get("execution_metadata")).get("failure_kind")
                    or ""
                )
                + " "
                + str(call.get("error_message") or call.get("output_summary") or "")
                for call in tool_calls
                if str(call.get("status") or "") in {"failed", "error", "blocked"}
            ],
        ]
    ).lower()
    timeout_markers = {
        "timeout",
        "timed out",
        "超时",
        "deadline",
        "total_timeout_exhausted",
        "attempts_exhausted",
    }
    dependency_markers = {
        "connection",
        "unavailable",
        "not configured",
        "not_configured",
        "refused",
        "依赖",
        "连接",
        "不可用",
        "未配置",
    }

    if status == "completed":
        category = "none"
        safe_terminal = True
    elif errors or failed_tools:
        category = (
            "dependency_timeout"
            if any(marker in failure_text for marker in timeout_markers)
            else "dependency_failure"
            if any(marker in failure_text for marker in dependency_markers)
            else "runtime_orchestration_failure"
        )
        safe_terminal = False
    elif missing_evidence:
        category = "evidence_insufficient"
        safe_terminal = status in REPORT_SAFE_INSUFFICIENT_EVIDENCE_STATUSES
    else:
        category = "policy_or_approval_boundary"
        safe_terminal = status in REPORT_SAFE_POLICY_BOUNDARY_STATUSES

    reasons: list[str] = []
    if missing_evidence:
        reasons.append("缺失证据：" + "、".join(missing_evidence))
    if failed_tools:
        reasons.append("失败工具：" + "、".join(failed_tools))
    if errors:
        reasons.append("运行错误：" + "；".join(errors[:3]))
    if not reasons and status != "completed":
        reasons.append(f"终态由策略或状态机边界确定：{status}")

    return {
        "category": category,
        "safe_terminal": safe_terminal,
        "needs_human": status in {"degraded", "needs_human", "incomplete", "blocked"}
        or not safe_terminal,
        "missing_evidence": missing_evidence,
        "failed_tools": failed_tools,
        "failed_tool_details": failed_tool_details,
        "errors": list(errors),
        "reasons": reasons,
    }


def _build_hypothesis_ranking(
    hypotheses: list[str],
    evidence_analysis: dict[str, Any],
    evidence: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    raw_ranking = evidence_analysis.get("hypothesis_ranking")
    if isinstance(raw_ranking, list) and raw_ranking:
        ranking = [dict(item) for item in raw_ranking if isinstance(item, dict)]
        return _ensure_hypothesis_evidence_links(ranking, evidence or [])

    fallback_ranking: list[dict[str, Any]] = []
    for index, item in enumerate(hypotheses, 1):
        fallback_ranking.append(
            {
                "hypothesis_id": f"hyp-fallback-{index}",
                "title": item,
                "description": item,
                "category": "unknown",
                "supporting_evidence_ids": [],
                "refuting_evidence_ids": [],
                "missing_evidence": [],
                "confidence": 0.22,
                "confidence_reason": "兼容旧版 hypotheses 字段生成，缺少按假设关联的证据矩阵明细。",
            }
        )
    return fallback_ranking


def _ensure_hypothesis_evidence_links(
    ranking: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop invalid evidence links without attaching unrelated supporting evidence."""
    valid_supporting_ids = set(_supporting_evidence_ids(evidence))
    valid_refuting_ids = {
        str(item.get("evidence_id"))
        for item in evidence
        if item.get("evidence_id")
        and str(item.get("stance") or "") == "refuting"
        and _evidence_is_usable_for_conclusion(item)
    }
    updated: list[dict[str, Any]] = []
    for item in ranking:
        copy = dict(item)
        raw_ids = copy.get("supporting_evidence_ids")
        copy["supporting_evidence_ids"] = [
            str(value) for value in raw_ids or [] if str(value) in valid_supporting_ids
        ]
        raw_refuting = copy.get("refuting_evidence_ids")
        copy["refuting_evidence_ids"] = [
            str(value) for value in raw_refuting or [] if str(value) in valid_refuting_ids
        ]
        updated.append(copy)
    return updated


def _supporting_evidence_ids(evidence: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("evidence_id"))
        for item in evidence
        if item.get("evidence_id")
        and str(item.get("stance") or "") == "supporting"
        and str(item.get("evidence_type") or "") != "risk"
        and _evidence_is_usable_for_conclusion(item)
    ][:5]


def _selected_root_cause_id(hypothesis_ranking: list[dict[str, Any]]) -> str:
    selected = _selected_root_cause_item(hypothesis_ranking)
    return str(selected.get("hypothesis_id") or "") if selected else ""


def _selected_root_cause_category(hypothesis_ranking: list[dict[str, Any]]) -> str:
    selected = _selected_root_cause_item(hypothesis_ranking)
    return str(selected.get("category") or "unknown") if selected else "unknown"


def _selected_root_cause_text(hypothesis_ranking: list[dict[str, Any]]) -> str:
    selected = _selected_root_cause_item(hypothesis_ranking)
    if not selected:
        return ""
    return str(selected.get("title") or selected.get("description") or "")


def _selected_root_cause_item(
    hypothesis_ranking: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select only a hypothesis with direct usable support."""
    for item in hypothesis_ranking:
        supporting = item.get("supporting_evidence_ids")
        if isinstance(supporting, list) and any(str(value).strip() for value in supporting):
            return item
    return None


def _evidence_is_usable_for_conclusion(evidence: dict[str, Any]) -> bool:
    raw_data = _as_dict(evidence.get("raw_data"))
    if raw_data.get("status") != "success":
        return False
    output = raw_data.get("output")
    if output is None or output in ("", [], {}):
        return False
    if isinstance(output, str) and output.strip().lower() in {"ok", "success", "empty", "no data"}:
        return False
    metadata = _as_dict(raw_data.get("metadata"))
    quality = _as_dict(metadata.get("evidence_quality"))
    if quality.get("usable", True) is False:
        return False
    return str(evidence.get("data_source") or "") not in {
        "failed",
        "not_configured",
        "manual_analysis",
        "llm_toolnode_fallback",
    }


def _compact_tool_call(record: dict[str, Any]) -> dict[str, Any]:
    payload = redact_sensitive_data(
        {
            "call_id": record.get("call_id", ""),
            "step_id": record.get("step_id", ""),
            "tool_name": record.get("tool_name", "unknown"),
            "status": record.get("status", "unknown"),
            "latency_ms": record.get("latency_ms", 0.0),
            "data_source": record.get("data_source", "unknown"),
            "input_summary": record.get("input_summary", ""),
            "output_summary": record.get("output_summary", ""),
            "output_artifact": record.get("output_artifact"),
            "risk_level": record.get("risk_level", "low"),
            "read_only": record.get("read_only", True),
            "invocation_kind": record.get("invocation_kind", "tool"),
            "actual_tool_invoked": record.get("actual_tool_invoked", True),
            "input_args": record.get("input_args", {}),
            "error_message": record.get("error_message"),
        }
    )
    return dict(payload) if isinstance(payload, dict) else {}


def _normalize_evidence_ids(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure report-local evidence references are unique and deterministic."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_counts: dict[str, int] = {}
    for item in evidence:
        copy = dict(item)
        evidence_id = str(copy.get("evidence_id") or "").strip()
        if not evidence_id:
            evidence_id = _derived_evidence_id(copy)
        if evidence_id in seen:
            duplicate_counts[evidence_id] = duplicate_counts.get(evidence_id, 0) + 1
            evidence_id = _derived_evidence_id(
                copy,
                base=evidence_id,
                occurrence=duplicate_counts[evidence_id],
            )
        while evidence_id in seen:
            duplicate_counts[evidence_id] = duplicate_counts.get(evidence_id, 0) + 1
            evidence_id = _derived_evidence_id(
                copy,
                base=evidence_id,
                occurrence=duplicate_counts[evidence_id],
            )
        seen.add(evidence_id)
        copy["evidence_id"] = evidence_id
        result.append(copy)
    return result


def _derived_evidence_id(
    evidence: dict[str, Any],
    *,
    base: str = "evd",
    occurrence: int = 0,
) -> str:
    raw_data = _as_dict(evidence.get("raw_data"))
    identity = {
        "base": base,
        "source_tool": evidence.get("source_tool"),
        "step_id": evidence.get("step_id"),
        "data_source": evidence.get("data_source"),
        "input_args": raw_data.get("input_args"),
        "output": raw_data.get("output"),
        "occurrence": occurrence,
    }
    digest = sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"{base}-{digest}"


def _report_title(incident: dict[str, Any]) -> str:
    title = str(incident.get("title") or "").strip()
    if title and title != "AIOps diagnosis request":
        return f"{title} 诊断报告"
    service_name = str(incident.get("service_name") or "unknown-service")
    return f"{service_name} AIOps 诊断报告"


def _build_summary(
    incident: dict[str, Any],
    evidence: list[dict[str, Any]],
    errors: list[str],
    status: str,
    *,
    root_cause: str,
) -> str:
    service_name = str(incident.get("service_name") or "unknown-service")
    symptom = str(incident.get("symptom") or incident.get("title") or "未提供明确症状")
    if root_cause:
        conclusion = root_cause
    elif errors:
        conclusion = "诊断过程中存在失败步骤，当前信息不足以确认根因"
    elif any(_evidence_is_usable_for_conclusion(item) for item in evidence):
        conclusion = "已收集到有效证据，但尚未形成有直接支持的根因结论"
    else:
        conclusion = "尚未收集到足够证据"
    return f"{service_name} 诊断状态为 {status}；症状：{symptom}；当前判断：{conclusion}。"


def _select_root_cause(errors: list[str]) -> str:
    if errors:
        return "诊断链路存在失败步骤，暂未形成明确根因"
    return "证据不足，暂未形成明确根因"


def _build_key_findings(
    hypotheses: list[str],
    evidence: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    errors: list[str],
) -> list[str]:
    findings: list[str] = []
    seen_tools: set[str] = set()
    for item in sorted(
        evidence,
        key=lambda value: (
            0 if value.get("stance") == "supporting" else 1,
            -float(value.get("confidence") or 0.0),
        ),
    ):
        if not _evidence_is_usable_for_conclusion(item):
            continue
        tool_name = str(item.get("source_tool") or "")
        if tool_name in seen_tools:
            continue
        finding = str(item.get("fact") or _evidence_summary(item)).strip()
        if finding and finding not in findings:
            findings.append(finding)
            if tool_name:
                seen_tools.add(tool_name)
        if len(findings) >= 4:
            break
    failed_tools = [call for call in tool_calls if call.get("status") == "failed"]
    if failed_tools:
        names = _dedupe_strings([str(call.get("tool_name") or "unknown") for call in failed_tools])
        findings.append(f"失败工具：{', '.join(names)}；对应证据缺失，需要人工复核。")
    elif errors:
        findings.append(errors[0])
    return findings[:5] or ["未形成明确关键发现"]


def _build_conclusion_alignment(
    *,
    root_cause: str,
    key_findings: list[str],
    remediation_suggestion: str,
    hypothesis_ranking: list[dict[str, Any]],
    selected_root_cause_id: str,
    evidence: list[dict[str, Any]],
    require_root_cause_alignment: bool,
) -> dict[str, Any]:
    """Link conclusion-level fields to evidence IDs or RAG citations."""
    root_evidence_ids = _selected_hypothesis_evidence_ids(
        hypothesis_ranking,
        selected_root_cause_id,
    )
    root_refs = _alignment_refs_from_evidence_ids(evidence, root_evidence_ids)
    finding_items = [
        _align_text_to_evidence(finding, evidence, prefer_supporting=False)
        for finding in key_findings
    ]
    remediation_refs = _alignment_refs_for_remediation(remediation_suggestion, evidence)

    fields: dict[str, Any] = {
        "root_cause": {
            "text": root_cause,
            "evidence_ids": root_refs["evidence_ids"],
            "citations": root_refs["citations"],
            "aligned": (
                bool(root_refs["evidence_ids"] or root_refs["citations"])
                if require_root_cause_alignment
                else True
            ),
            "claim_type": "root_cause" if require_root_cause_alignment else "insufficiency",
        },
        "key_findings": finding_items,
        "remediation_suggestion": {
            "text": remediation_suggestion,
            "evidence_ids": remediation_refs["evidence_ids"],
            "citations": remediation_refs["citations"],
            "aligned": bool(remediation_refs["evidence_ids"] or remediation_refs["citations"]),
        },
    }

    missing = []
    if not fields["root_cause"]["aligned"]:
        missing.append("root_cause")
    if any(not item["aligned"] for item in finding_items):
        missing.append("key_findings")
    if not fields["remediation_suggestion"]["aligned"]:
        missing.append("remediation_suggestion")

    return {
        "status": "aligned" if not missing else "needs_human_confirmation",
        "required_fields": ["root_cause", "key_findings", "remediation_suggestion"],
        "missing_fields": missing,
        "fields": fields,
    }


def _apply_conclusion_alignment_gate(
    *,
    requested_status: str,
    alignment: dict[str, Any],
) -> tuple[str, list[str]]:
    if requested_status != "completed":
        return requested_status, []
    missing = [str(item) for item in alignment.get("missing_fields", []) if str(item).strip()]
    if not missing:
        return requested_status, []
    return (
        "needs_human",
        [
            "报告由 completed 降级为 needs_human：关键结论缺少 evidence_id 或 RAG citation 回链，"
            f"字段={', '.join(missing)}。"
        ],
    )


def _downgrade_unaligned_text(
    value: str,
    alignment: dict[str, Any],
    field_name: str,
) -> str:
    if field_name not in set(alignment.get("missing_fields", [])):
        return value
    return f"待人工确认：{value or '当前结论缺少稳定证据回链。'}"


def _downgrade_unaligned_findings(
    findings: list[str],
    alignment: dict[str, Any],
) -> list[str]:
    items = alignment.get("fields", {}).get("key_findings", [])
    if not isinstance(items, list):
        return findings
    result = []
    for index, finding in enumerate(findings):
        item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
        if item.get("aligned"):
            result.append(finding)
        else:
            result.append(f"待人工确认：{finding}")
    return result


def _selected_hypothesis_evidence_ids(
    hypothesis_ranking: list[dict[str, Any]],
    selected_root_cause_id: str,
) -> list[str]:
    for item in hypothesis_ranking:
        if selected_root_cause_id and item.get("hypothesis_id") != selected_root_cause_id:
            continue
        raw_ids = item.get("supporting_evidence_ids")
        if isinstance(raw_ids, list):
            return [str(value) for value in raw_ids if str(value).strip()]
    return []


def _alignment_refs_from_evidence_ids(
    evidence: list[dict[str, Any]],
    evidence_ids: list[str],
) -> dict[str, Any]:
    id_set = set(evidence_ids)
    matched = [
        item
        for item in evidence
        if str(item.get("evidence_id") or "") in id_set
    ]
    matched = [
        item
        for item in matched
        if _evidence_is_usable_for_conclusion(item)
        and str(item.get("stance") or "") == "supporting"
    ]
    citations = _rag_citations_from_evidence(matched)
    return {
        "evidence_ids": [
            str(item.get("evidence_id"))
            for item in matched
            if str(item.get("evidence_id") or "").strip()
        ],
        "citations": citations,
    }


def _align_text_to_evidence(
    text: str,
    evidence: list[dict[str, Any]],
    *,
    prefer_supporting: bool,
) -> dict[str, Any]:
    terms = _alignment_terms(text)
    candidates = []
    for item in evidence:
        if not _evidence_is_usable_for_conclusion(item):
            continue
        if str(item.get("stance") or "") not in {"supporting", "neutral"}:
            continue
        if prefer_supporting and str(item.get("stance") or "") != "supporting":
            continue
        haystack = _evidence_alignment_text(item)
        if terms and not any(term in haystack for term in terms):
            continue
        candidates.append(item)
    if not candidates and terms:
        candidates = [
            item
            for item in evidence
            if _evidence_is_usable_for_conclusion(item)
            and str(item.get("stance") or "") in {"supporting", "neutral"}
            if any(term in _evidence_alignment_text(item) for term in terms)
        ]
    if not candidates and not terms:
        candidates = [
            item
            for item in evidence
            if str(item.get("stance") or "") == "supporting"
            and _evidence_is_usable_for_conclusion(item)
        ]
    if not candidates:
        return {
            "text": text,
            "evidence_ids": [],
            "citations": [],
            "aligned": False,
        }
    candidates = candidates[:3]
    citations = _rag_citations_from_evidence(candidates)
    evidence_ids = [
        str(item.get("evidence_id"))
        for item in candidates
        if str(item.get("evidence_id") or "").strip()
    ]
    return {
        "text": text,
        "evidence_ids": evidence_ids,
        "citations": citations,
        "aligned": bool(evidence_ids or citations),
    }


def _alignment_refs_for_remediation(
    remediation_suggestion: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    reference_evidence = [
        item
        for item in evidence
        if _evidence_is_usable_for_conclusion(item)
        and str(item.get("stance") or "") in {"supporting", "neutral"}
        and (
            str(item.get("evidence_type") or "") in {"runbook", "ticket", "deploy_history", "risk"}
            or str(item.get("source_tool") or "")
            in {
                "search_runbook",
                "retrieve_knowledge",
                "search_history_ticket",
                "query_deploy_history",
            }
        )
    ]
    if not reference_evidence:
        aligned = _align_text_to_evidence(remediation_suggestion, evidence, prefer_supporting=True)
        return {"evidence_ids": aligned["evidence_ids"], "citations": aligned["citations"]}
    reference_evidence = reference_evidence[:4]
    return {
        "evidence_ids": [
            str(item.get("evidence_id"))
            for item in reference_evidence
            if str(item.get("evidence_id") or "").strip()
        ],
        "citations": _rag_citations_from_evidence(reference_evidence),
    }


def _alignment_terms(text: str) -> list[str]:
    lowered = text.lower()
    tokens = [
        "redis",
        "maxclients",
        "connected_clients",
        "mysql",
        "slow",
        "query",
        "pool",
        "p95",
        "5xx",
        "timeout",
        "k8s",
        "pod",
        "oom",
        "runbook",
        "ticket",
        "deploy",
        "approval",
    ]
    return [token for token in tokens if token in lowered]


def _evidence_alignment_text(item: dict[str, Any]) -> str:
    raw_data = _as_dict(item.get("raw_data"))
    output = _as_dict(raw_data.get("output"))
    parts = [
        item.get("evidence_id"),
        item.get("source_tool"),
        item.get("data_source"),
        item.get("evidence_type"),
        item.get("summary"),
        item.get("fact"),
        item.get("inference"),
        item.get("uncertainty"),
        output.get("summary"),
    ]
    return " ".join(str(part).lower() for part in parts if part)


def _rag_citations_from_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in evidence:
        raw_data = _as_dict(item.get("raw_data"))
        output = _as_dict(raw_data.get("output"))
        for payload in (raw_data, output):
            results = payload.get("retrieval_results")
            if not isinstance(results, list):
                continue
            for result in results:
                if not isinstance(result, dict):
                    continue
                source_file = str(result.get("source_file") or "").strip()
                chunk_id = str(result.get("chunk_id") or "").strip()
                if not source_file or not chunk_id:
                    continue
                key = f"{source_file}#{chunk_id}"
                if key in seen:
                    continue
                seen.add(key)
                citations.append({"source_file": source_file, "chunk_id": chunk_id})
    return citations[:5]


def _build_confirmed_facts(
    evidence: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> list[str]:
    facts: list[str] = []
    for item in evidence:
        if not _evidence_is_usable_for_conclusion(item):
            continue
        fact = str(item.get("fact") or "").strip()
        if fact:
            facts.append(fact)
            continue
        summary = _evidence_summary(item)
        source = item.get("data_source") or item.get("source_tool") or "unknown"
        status = _as_dict(item.get("raw_data")).get("status", "unknown")
        if summary:
            facts.append(f"{summary}；来源={source}；状态={status}")
    for call in tool_calls:
        if call.get("status") == "failed":
            facts.append(
                f"{call.get('tool_name', 'unknown')} 调用失败；来源={call.get('data_source', 'unknown')}；"
                f"原因={call.get('error_message') or '未知错误'}"
            )
    return _dedupe_strings(facts)[:10] or ["尚未确认足够事实"]


def _build_inferred_conclusions(
    hypotheses: list[str],
    evidence: list[dict[str, Any]],
) -> list[str]:
    conclusions: list[str] = []
    for item in evidence:
        if not _evidence_is_usable_for_conclusion(item):
            continue
        inference = str(item.get("inference") or "").strip()
        if inference:
            conclusions.append(inference)
    return _dedupe_strings(conclusions)[:8] or ["当前证据不足以形成稳定推断"]


def _build_next_steps(
    evidence: list[dict[str, Any]],
    errors: list[str],
    risk_summary: dict[str, Any],
    pending_approval: dict[str, Any],
) -> list[str]:
    steps = [
        str(item.get("next_step") or "").strip()
        for item in evidence
        if str(item.get("next_step") or "").strip()
    ]
    if errors:
        steps.append("优先修复失败工具链路后重试关键证据采集。")
    if pending_approval or risk_summary.get("need_approval"):
        steps.append("在人工审批中心完成决策，审批通过后仍由人工按变更流程执行。")
    if risk_summary.get("policy") == "forbidden":
        steps.append("禁止动作不得自动执行，转人工变更流程复核。")
    return _dedupe_strings(steps)[:8] or ["继续补充指标、日志、依赖状态和 Runbook 证据。"]


def _build_timeline(
    trace_events: list[TraceEvent],
    past_steps: Any,
) -> list[dict[str, Any]]:
    if trace_events:
        return [
            {
                "time": event.created_at.isoformat(),
                "event_type": event.event_type,
                "node_name": event.node_name,
                "step_id": event.step_id,
                "status": event.status,
                "summary": event.output_summary or event.input_summary,
            }
            for event in sorted(trace_events, key=lambda item: item.created_at)
        ]

    if not isinstance(past_steps, list):
        return []
    timeline: list[dict[str, Any]] = []
    for index, item in enumerate(past_steps, 1):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        timeline.append(
            {
                "index": index,
                "event_type": "step",
                "node_name": "executor",
                "status": "success",
                "summary": f"{item[0]} -> {str(item[1])[:300]}",
            }
        )
    return timeline


def _build_impact(
    incident: dict[str, Any],
    evidence: list[dict[str, Any]],
    risk_summary: dict[str, Any],
) -> str:
    service_name = str(incident.get("service_name") or "unknown-service")
    severity = str(incident.get("severity") or "P2")
    environment = str(incident.get("environment") or "unknown")
    risk_level = risk_summary.get("risk_level")
    evidence_count = len(evidence)
    impact = f"{severity} 级别事件，影响服务 {service_name}，环境 {environment}，已采集 {evidence_count} 条证据"
    if risk_level:
        impact += f"，当前风险等级 {risk_level}"
    return impact + "。"


def _build_remediation(
    *,
    state: dict[str, Any],
    evidence: list[dict[str, Any]],
    risk_summary: dict[str, Any],
    pending_approval: dict[str, Any],
) -> str:
    explicit = str(state.get("remediation_suggestion") or "").strip()
    if explicit:
        return explicit
    if pending_approval:
        action = pending_approval.get("action") or risk_summary.get("action") or "后续处置动作"
        return f"等待人工审批后再执行：{action}。审批前保持只读排查和监控观察。"
    if risk_summary.get("policy") == "forbidden":
        return "已拦截高风险动作，请通过人工变更流程重新评估，不允许 Agent 自动执行。"

    summaries = "；".join(
        _evidence_summary(item) for item in evidence[:3] if _evidence_summary(item)
    )
    if "Redis" in summaries or "redis" in summaries:
        return "优先检查 Redis 连接池、maxclients、慢查询和上游重试策略，并按变更流程处理容量或配置问题。"
    if any(
        token in summaries for token in ["MySQL", "mysql", "SQL", "slow query", "慢查询", "连接池"]
    ):
        return (
            "诊断阶段保持只读：先确认慢 SQL digest、EXPLAIN、连接池等待、活跃连接和锁等待，并关联最近发布。"
            "短期可限流或降级高成本查询路径；执行 SQL 改写、加索引、调整连接池/数据库参数或重启数据库前必须走人工审批和变更窗口。"
        )
    if evidence:
        return "基于已采集证据按 Runbook 继续处置，先验证影响面，再执行低风险缓解动作。"
    return "继续补充指标、日志和依赖状态证据，在证据不足时升级人工处理。"


def _build_prevention(evidence: list[dict[str, Any]], errors: list[str]) -> str:
    suggestions = [
        "为关键服务补充指标、日志和依赖状态的统一排障视图。",
        "将高频故障的 Runbook 固化为只读诊断步骤和审批化处置步骤。",
    ]
    summaries = "；".join(_evidence_summary(item) for item in evidence)
    if "Redis" in summaries or "redis" in summaries:
        suggestions.append("为 Redis 连接数、拒绝连接和慢查询配置提前预警阈值。")
    if errors:
        suggestions.append("修复失败工具链路，避免诊断时证据缺口扩大。")
    return " ".join(suggestions)


def _build_trace_summary(trace_events: list[TraceEvent]) -> dict[str, Any]:
    by_type = Counter(event.event_type for event in trace_events)
    failed = [event for event in trace_events if event.status in {"failed", "blocked"}]
    last_event = max(trace_events, key=lambda event: event.created_at, default=None)
    return {
        "event_count": len(trace_events),
        "by_type": dict(by_type),
        "failed_or_blocked_count": len(failed),
        "last_event_at": last_event.created_at.isoformat() if last_event else "",
    }


def _approval_status(
    pending_approval: dict[str, Any],
    risk_summary: dict[str, Any],
) -> str:
    if pending_approval:
        return str(pending_approval.get("status") or "pending")
    if risk_summary.get("policy") == "forbidden":
        return "forbidden"
    if risk_summary.get("need_approval"):
        return "required"
    return "not_required"


def _manual_action_required(
    *,
    pending_approval: dict[str, Any],
    risk_summary: dict[str, Any],
    status: str,
) -> bool:
    return report_requires_manual_action(
        status,
        has_pending_approval=bool(pending_approval),
        risk_requires_approval=bool(risk_summary.get("need_approval")),
        risk_forbidden=risk_summary.get("policy") == "forbidden",
    )


def _build_change_plan_snapshot(
    state: dict[str, Any],
    pending_approval: dict[str, Any],
    risk_summary: dict[str, Any],
) -> dict[str, Any]:
    """Extract a serialized ChangePlan draft from state, approval, or risk metadata."""
    direct = _as_dict(state.get("change_plan"))
    if direct:
        return direct

    approval_plan = _as_dict(pending_approval.get("change_plan"))
    if approval_plan:
        return approval_plan

    metadata = _as_dict(pending_approval.get("metadata"))
    metadata_plan = _as_dict(metadata.get("change_plan"))
    if metadata_plan:
        return metadata_plan

    risk_plan = _as_dict(risk_summary.get("change_plan"))
    if risk_plan:
        return risk_plan

    return {}


def _evidence_summary(evidence: dict[str, Any]) -> str:
    summary = str(evidence.get("summary") or "").strip()
    if summary:
        return summary
    raw_data = _as_dict(evidence.get("raw_data"))
    output = _as_dict(raw_data.get("output"))
    return str(output.get("summary") or "").strip()


def _build_dependency_signals(
    evidence: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Advanced dependency panels were removed from the campus-recruiting mainline."""
    return []
