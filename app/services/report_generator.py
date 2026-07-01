"""Deterministic report generation for AIOps diagnosis workflows."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.models.incident import utc_now
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.aiops_store import create_aiops_store
from app.services.change_execution_read_models import (
    build_change_execution_stages,
    change_execution_next_steps,
    change_execution_uncertainties,
)
from app.services.change_plan_builder import update_change_plan_status
from app.services.incident_lifecycle import (
    incident_status_from_report_status,
    manual_action_required_from_change_execution,
    status_from_change_execution,
)
from app.services.incident_state_builder import build_incident_state_from_report
from app.services.legacy_migration import resolve_legacy_jsonl_path
from app.services.sqlite_store import resolve_sqlite_path
from app.services.trace_service import trace_service


class ReportGenerator:
    """Generate and persist queryable diagnosis reports."""

    def __init__(
        self,
        storage_path: str | Path | None = None,
        *,
        legacy_storage_path: str | Path | None = None,
    ):
        raw_storage_path = Path(storage_path) if storage_path is not None else None
        self.database_path = resolve_sqlite_path(raw_storage_path)
        self._store = create_aiops_store(raw_storage_path)
        self.storage_path = getattr(self._store, "storage_path", self.database_path)
        self._migrate_legacy_jsonl(
            legacy_storage_path
            if legacy_storage_path is not None
            else resolve_legacy_jsonl_path(raw_storage_path, "reports.jsonl")
        )

    def generate_from_state(
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
        evidence = _dict_list(state.get("gathered_evidence"))
        tool_calls = [
            _compact_tool_call(record) for record in _dict_list(state.get("tool_call_records"))
        ]
        hypotheses = [str(item) for item in state.get("hypotheses", []) if item]
        risk_summary = _as_dict(state.get("risk_assessment"))
        pending_approval = _as_dict(state.get("pending_approval"))
        evidence_analysis = _as_dict(state.get("evidence_analysis"))
        errors = [str(error) for error in state.get("errors", []) if error]
        warnings = [str(warning) for warning in state.get("warnings", []) if warning]
        hypothesis_ranking = _build_hypothesis_ranking(hypotheses, evidence_analysis)
        selected_root_cause_id = _selected_root_cause_id(hypothesis_ranking)
        selected_root_cause = _selected_root_cause_text(hypothesis_ranking)

        events = (
            list(trace_events)
            if trace_events is not None
            else trace_service.list_events(incident_id=incident_id)
        )
        approval_status = _approval_status(pending_approval, risk_summary)
        manual_action_required = _manual_action_required(
            pending_approval=pending_approval,
            risk_summary=risk_summary,
            status=status,
        )

        report = DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            title=_report_title(incident),
            service_name=str(incident.get("service_name") or "unknown-service"),
            severity=str(incident.get("severity") or "P2"),
            environment=str(incident.get("environment") or "unknown"),
            status=status,
            summary=_build_summary(incident, evidence, hypotheses, errors, status),
            root_cause=selected_root_cause or _select_root_cause(hypotheses, evidence, errors),
            hypotheses=hypotheses,
            hypothesis_ranking=hypothesis_ranking,
            selected_root_cause_id=selected_root_cause_id,
            evidence=evidence,
            key_findings=_build_key_findings(hypotheses, evidence, tool_calls, errors),
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
            approval_decision=_build_approval_decision(pending_approval, risk_summary),
            change_plan=_build_change_plan_snapshot(state, pending_approval, risk_summary),
            remediation_suggestion=_build_remediation(
                state=state,
                evidence=evidence,
                risk_summary=risk_summary,
                pending_approval=pending_approval,
            ),
            prevention=_build_prevention(evidence, errors),
            trace_summary=_build_trace_summary(events),
            errors=errors,
            warnings=warnings,
            evidence_profile=_build_evidence_profile(evidence, evidence_analysis),
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
        report = report.model_copy(update={"markdown": _render_markdown(report)})
        self.save_report(report)
        if trace_events is None:
            _record_report_generated(report, existing_events=events)
        return report

    def save_report(self, report: DiagnosisReport) -> DiagnosisReport:
        """Persist a report and keep the latest copy in memory."""
        self._store.save_report(report)
        self._store.save_incident_state(
            build_incident_state_from_report(
                report=report,
                status=_incident_status_from_report_status(report.status),
                status_reason=f"Diagnosis report saved: {report.status}",
            )
        )
        return report

    def get_report(self, incident_id: str) -> DiagnosisReport | None:
        """Return the latest report for one incident."""
        return self._store.get_latest_report(incident_id)

    def mark_approval_decided(
        self,
        *,
        incident_id: str,
        approval_status: str,
        decided_by: str | None = None,
        reason: str = "",
        approval_request: dict[str, Any] | None = None,
    ) -> DiagnosisReport | None:
        """Update the latest waiting-approval report after a human decision."""
        report = self.get_report(incident_id)
        if report is None:
            return None

        if approval_status not in {"approved", "rejected"}:
            return report

        status = f"approval_{approval_status}"
        decision_text = "审批已通过" if approval_status == "approved" else "审批已拒绝"
        actor_text = f"，处理人：{decided_by}" if decided_by else ""
        reason_text = f"，原因：{reason}" if reason else ""
        risk_summary = dict(report.risk_summary or {})
        decision_snapshot = _build_approval_decision(
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
        change_plan = update_change_plan_status(dict(report.change_plan or {}), approval_status)
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
            summary = (
                f"{summary} {decision_text}，审批通过后进入 pre-check、dry-run、"
                "sandbox 或人工执行记录。"
            )

        updated = report.model_copy(
            update={
                "status": status,
                "approval_status": approval_status,
                "approval_decision": decision_snapshot,
                "risk_summary": risk_summary,
                "change_plan": change_plan,
                "manual_action_required": True,
                "summary": summary,
                "uncertainties": _dedupe_strings(uncertainties)[:8],
            }
        )
        updated = updated.model_copy(update={"markdown": _render_markdown(updated)})
        self.save_report(updated)
        return updated

    def mark_change_execution_updated(
        self,
        *,
        incident_id: str,
        execution: dict[str, Any],
    ) -> DiagnosisReport | None:
        """Best-effort update for the latest report with safe change workflow state."""
        report = self.get_report(incident_id)
        if report is None:
            return None

        executions = _upsert_change_execution_snapshot(report.change_executions, execution)
        risk_summary = dict(report.risk_summary or {})
        risk_summary["change_execution"] = execution
        status = str(execution.get("status") or "")
        report_status = _report_status_from_change_execution(status)
        approval_decision = _change_execution_approval_decision(
            report.approval_decision,
            execution,
        )
        manual_action_required = _manual_action_required_from_change_execution(
            status,
            fallback=report.manual_action_required,
        )
        updated = report.model_copy(
            update={
                "status": report_status,
                "change_executions": executions,
                "risk_summary": risk_summary,
                "approval_status": (
                    "approved" if execution.get("approval_id") else report.approval_status
                ),
                "approval_decision": approval_decision,
                "manual_action_required": manual_action_required,
                "summary": _append_change_execution_summary(report.summary, status),
                "next_steps": change_execution_next_steps(report.next_steps, status),
                "uncertainties": change_execution_uncertainties(
                    report.uncertainties,
                    status,
                ),
            }
        )
        updated = updated.model_copy(update={"markdown": _render_markdown(updated)})
        self.save_report(updated)
        return updated

    def list_reports(self) -> list[DiagnosisReport]:
        """Return all known latest reports sorted by creation time."""
        return self._store.list_latest_reports()

    def _migrate_legacy_jsonl(self, legacy_storage_path: str | Path | None) -> None:
        if legacy_storage_path is None:
            return
        path = Path(legacy_storage_path)
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                payload = record.get("report") or record
                report = DiagnosisReport.model_validate(payload)
            except Exception:
                continue
            self._store.save_report(report)


def _extract_incident_id(state: dict[str, Any]) -> str:
    incident = _as_dict(state.get("incident"))
    return str(incident.get("incident_id") or "incident-unknown")


def _incident_status_from_report_status(status: str) -> str:
    return incident_status_from_report_status(status)


def _report_status_from_change_execution(status: str) -> str:
    return status_from_change_execution(status)


def _manual_action_required_from_change_execution(status: str, *, fallback: bool) -> bool:
    return manual_action_required_from_change_execution(status, fallback=fallback)


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
        return payload if isinstance(payload, dict) else {}
    return {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            result.append(dict(item))
        elif hasattr(item, "model_dump"):
            result.append(item.model_dump(mode="json"))
    return result


def _build_hypothesis_ranking(
    hypotheses: list[str],
    evidence_analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_ranking = evidence_analysis.get("hypothesis_ranking")
    if isinstance(raw_ranking, list) and raw_ranking:
        return [dict(item) for item in raw_ranking if isinstance(item, dict)]

    ranking: list[dict[str, Any]] = []
    for index, item in enumerate(hypotheses, 1):
        ranking.append(
            {
                "hypothesis_id": f"hyp-fallback-{index}",
                "title": item,
                "description": item,
                "category": "unknown",
                "supporting_evidence_ids": [],
                "refuting_evidence_ids": [],
                "missing_evidence": [],
                "confidence": 0.45,
                "confidence_reason": "兼容旧版 hypotheses 字段生成，缺少证据矩阵明细。",
            }
        )
    return ranking


def _selected_root_cause_id(hypothesis_ranking: list[dict[str, Any]]) -> str:
    if not hypothesis_ranking:
        return ""
    return str(hypothesis_ranking[0].get("hypothesis_id") or "")


def _selected_root_cause_text(hypothesis_ranking: list[dict[str, Any]]) -> str:
    if not hypothesis_ranking:
        return ""
    top = hypothesis_ranking[0]
    return str(top.get("title") or top.get("description") or "")


def _compact_tool_call(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "call_id": record.get("call_id", ""),
        "step_id": record.get("step_id", ""),
        "tool_name": record.get("tool_name", "unknown"),
        "status": record.get("status", "unknown"),
        "latency_ms": record.get("latency_ms", 0.0),
        "data_source": record.get("data_source", "unknown"),
        "input_summary": record.get("input_summary", ""),
        "output_summary": record.get("output_summary", ""),
        "risk_level": record.get("risk_level", "low"),
        "read_only": record.get("read_only", True),
        "input_args": record.get("input_args", {}),
        "error_message": record.get("error_message"),
    }


def _report_title(incident: dict[str, Any]) -> str:
    title = str(incident.get("title") or "").strip()
    if title and title != "AIOps diagnosis request":
        return f"{title} 诊断报告"
    service_name = str(incident.get("service_name") or "unknown-service")
    return f"{service_name} AIOps 诊断报告"


def _build_summary(
    incident: dict[str, Any],
    evidence: list[dict[str, Any]],
    hypotheses: list[str],
    errors: list[str],
    status: str,
) -> str:
    service_name = str(incident.get("service_name") or "unknown-service")
    symptom = str(incident.get("symptom") or incident.get("title") or "未提供明确症状")
    if hypotheses:
        conclusion = hypotheses[0]
    elif evidence:
        conclusion = _evidence_summary(evidence[0])
    elif errors:
        conclusion = "诊断过程中存在失败步骤，当前信息不足以确认根因"
    else:
        conclusion = "尚未收集到足够证据"
    return f"{service_name} 诊断状态为 {status}；症状：{symptom}；当前判断：{conclusion}。"


def _select_root_cause(
    hypotheses: list[str],
    evidence: list[dict[str, Any]],
    errors: list[str],
) -> str:
    if hypotheses:
        return hypotheses[0]
    for item in evidence:
        summary = _evidence_summary(item)
        if summary:
            return summary
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
    findings.extend(hypotheses[:3])
    for item in evidence[:5]:
        summary = _evidence_summary(item)
        if summary and summary not in findings:
            findings.append(summary)
    failed_tools = [call for call in tool_calls if call.get("status") == "failed"]
    if failed_tools:
        findings.append(f"{len(failed_tools)} 次工具调用失败，需要人工复核数据完整性")
    findings.extend(errors[:2])
    return findings[:8] or ["未形成明确关键发现"]


def _build_confirmed_facts(
    evidence: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> list[str]:
    facts: list[str] = []
    for item in evidence:
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
    conclusions.extend(hypotheses[:3])
    for item in evidence:
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


def _build_evidence_profile(
    evidence: list[dict[str, Any]],
    evidence_analysis: dict[str, Any],
) -> dict[str, Any]:
    profile = _as_dict(evidence_analysis.get("evidence_profile"))
    if profile:
        return profile

    by_type: dict[str, int] = {}
    by_stance: dict[str, int] = {}
    for item in evidence:
        evidence_type = str(item.get("evidence_type") or "unknown")
        stance = str(item.get("stance") or "neutral")
        by_type[evidence_type] = by_type.get(evidence_type, 0) + 1
        by_stance[stance] = by_stance.get(stance, 0) + 1
    return {"by_type": by_type, "by_stance": by_stance}


def _build_confidence_reason(
    evidence: list[dict[str, Any]],
    evidence_analysis: dict[str, Any],
    errors: list[str],
) -> str:
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


def _build_uncertainties(
    evidence_analysis: dict[str, Any],
    errors: list[str],
    warnings: list[str],
    risk_summary: dict[str, Any],
    status: str,
) -> list[str]:
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
    return bool(
        pending_approval
        or risk_summary.get("need_approval")
        or risk_summary.get("policy") == "forbidden"
        or status in {"waiting_approval", "blocked", "escalated"}
    )


def _build_approval_decision(
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


def _calculate_confidence(
    evidence: list[dict[str, Any]],
    errors: list[str],
    manual_action_required: bool,
    evidence_analysis: dict[str, Any] | None = None,
) -> float:
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
    source_quality_cap = _source_quality_confidence_cap(evidence, analysis)
    if source_quality_cap is not None:
        base = min(base, source_quality_cap)
    return round(max(0.0, min(1.0, base)), 2)


def _top_hypothesis_confidence(evidence_analysis: dict[str, Any]) -> float | None:
    ranking = evidence_analysis.get("hypothesis_ranking")
    if not isinstance(ranking, list) or not ranking:
        return None
    top = ranking[0] if isinstance(ranking[0], dict) else {}
    confidence = top.get("confidence")
    return float(confidence) if isinstance(confidence, int | float) else None


def _source_quality_confidence_cap(
    evidence: list[dict[str, Any]],
    evidence_analysis: dict[str, Any],
) -> float | None:
    profile = _as_dict(evidence_analysis.get("evidence_profile"))
    source_quality = str(profile.get("source_quality") or "")
    if source_quality == "fallback_only":
        return 0.5
    if source_quality == "mixed_with_fallback":
        return 0.72

    diagnostic_success = [
        item
        for item in evidence
        if str(item.get("evidence_type") or "") not in {"runbook", "risk"}
        and _as_dict(item.get("raw_data")).get("status") == "success"
    ]
    if not diagnostic_success:
        return None

    trusted_sources = {
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
    fallback_sources = {
        "mock",
        "not_configured",
        "failed",
        "manual_analysis",
        "llm_toolnode_fallback",
        "rule_based",
    }
    degraded_sources = {"mcp_monitor_mixed", "unknown"}
    trusted_count = 0
    fallback_count = 0
    degraded_count = 0
    for item in diagnostic_success:
        data_source = str(item.get("data_source") or "").strip().lower()
        raw_data = _as_dict(item.get("raw_data"))
        output = _as_dict(raw_data.get("output"))
        if not data_source or data_source == "unknown":
            data_source = (
                str(output.get("source") or raw_data.get("source") or "unknown").strip().lower()
            )
        if data_source in trusted_sources:
            trusted_count += 1
        elif data_source in fallback_sources:
            fallback_count += 1
        elif data_source in degraded_sources:
            degraded_count += 1
        else:
            degraded_count += 1

    low_quality_count = fallback_count + degraded_count
    if trusted_count == 0 and low_quality_count:
        return 0.5
    if low_quality_count:
        return 0.72
    return None


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


def _evidence_summary(evidence: dict[str, Any]) -> str:
    summary = str(evidence.get("summary") or "").strip()
    if summary:
        return summary
    raw_data = _as_dict(evidence.get("raw_data"))
    output = _as_dict(raw_data.get("output"))
    return str(output.get("summary") or "").strip()


def _render_markdown(report: DiagnosisReport) -> str:
    risk_level = report.risk_summary.get("risk_level", "low")
    risk_policy = report.risk_summary.get("policy", "allow")
    approval_line = (
        "当前状态：等待人工审批。"
        if report.approval_status == "pending"
        else f"审批状态：{report.approval_status}。"
    )
    return "\n".join(
        [
            f"# {report.title}",
            "",
            "## 摘要",
            report.summary or "暂无摘要。",
            "",
            "## 根因判断",
            report.root_cause or "暂未形成明确根因。",
            "",
            "## 根因假设矩阵",
            _render_hypothesis_ranking(report),
            "",
            "## 已确认事实",
            _render_bullets(report.confirmed_facts),
            "",
            "## 推断结论",
            _render_bullets(report.inferred_conclusions),
            "",
            "## 影响范围",
            report.impact or "暂无影响范围信息。",
            "",
            "## 关键证据",
            _render_bullets(report.key_findings),
            "",
            "## 证据质量",
            _render_evidence_quality(report),
            "",
            "## 不确定性",
            _render_bullets(report.uncertainties) if report.uncertainties else "- 暂无",
            "",
            "## 运行告警",
            _render_bullets(report.warnings) if report.warnings else "- 暂无",
            "",
            "## 下一步建议",
            _render_bullets(report.next_steps),
            "",
            "## Runbook 引用",
            _render_runbook_references(report.evidence),
            "",
            "## 工具调用摘要",
            _render_tool_calls(report.tool_calls),
            "",
            "## Tracing 与消息队列证据",
            _render_dependency_signals(report.dependency_signals),
            "",
            "## 风险与审批",
            f"- 风险等级：{risk_level}",
            f"- 策略：{risk_policy}",
            f"- {approval_line}",
            _render_approval_decision(report),
            f"- 是否需要人工动作：{'是' if report.manual_action_required else '否'}",
            "",
            "## 变更计划草案",
            _render_change_plan(report),
            "",
            "## 安全变更执行",
            _render_change_executions(report),
            "",
            "## Trace 摘要",
            f"- trace_id：{report.trace_id or 'unknown'}",
            f"- 事件数：{report.trace_summary.get('event_count', 0)}",
            f"- 异常或阻断事件数：{report.trace_summary.get('failed_or_blocked_count', 0)}",
            "",
            "## 处理建议",
            report.remediation_suggestion or "暂无处理建议。",
            "",
            "## 人工动作与回滚边界",
            _render_manual_action_boundary(report),
            "",
            "## 预防建议",
            report.prevention or "暂无预防建议。",
            "",
            f"> 置信度原因：{report.confidence_reason}",
            f"> 报告置信度：{report.confidence:.2f}",
        ]
    )


def _upsert_change_execution_snapshot(
    existing: list[dict[str, Any]],
    execution: dict[str, Any],
) -> list[dict[str, Any]]:
    execution_id = str(execution.get("change_execution_id") or "")
    snapshots = [dict(item) for item in existing if isinstance(item, dict)]
    if not execution_id:
        return (snapshots + [execution])[-10:]
    replaced = False
    for index, item in enumerate(snapshots):
        if str(item.get("change_execution_id") or "") == execution_id:
            snapshots[index] = execution
            replaced = True
            break
    if not replaced:
        snapshots.append(execution)
    return snapshots[-10:]


def _append_change_execution_summary(summary: str, status: str) -> str:
    status_text = _report_status_from_change_execution(status)
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


def _render_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 无"


def _render_hypothesis_ranking(report: DiagnosisReport) -> str:
    if not report.hypothesis_ranking:
        return "- 暂无根因假设排序"

    lines: list[str] = []
    for index, item in enumerate(report.hypothesis_ranking[:6], 1):
        selected = "（选中）" if item.get("hypothesis_id") == report.selected_root_cause_id else ""
        lines.extend(
            [
                f"{index}. {item.get('title') or item.get('description') or '未命名假设'}{selected}",
                f"   - 分类：{item.get('category', 'unknown')}；置信度：{float(item.get('confidence') or 0.0):.2f}",
                f"   - 支持证据：{_render_inline_list(item.get('supporting_evidence_ids'))}",
                f"   - 反驳证据：{_render_inline_list(item.get('refuting_evidence_ids'))}",
                f"   - 缺失证据：{_render_inline_list(item.get('missing_evidence'))}",
                f"   - 置信度原因：{item.get('confidence_reason') or '未说明'}",
            ]
        )
    return "\n".join(lines)


def _render_change_plan(report: DiagnosisReport) -> str:
    plan = report.change_plan or {}
    if not plan:
        return "- 无待审批变更计划；如需生产写操作，必须另行生成审批和变更计划。"

    return "\n".join(
        [
            f"- 计划ID：{plan.get('change_plan_id') or '未记录'}",
            f"- 状态：{plan.get('status') or 'draft'}",
            f"- 动作：{plan.get('action') or '未记录'}",
            f"- 风险等级：{plan.get('risk_level') or 'medium'}",
            "- 前置检查：",
            _render_indented_bullets(plan.get("pre_checklist")),
            "- 人工执行步骤：",
            _render_indented_bullets(plan.get("execution_steps")),
            "- 回滚步骤：",
            _render_indented_bullets(plan.get("rollback_steps")),
            "- 验证步骤：",
            _render_indented_bullets(plan.get("verification_steps")),
            "- 边界：Agent 只生成建议和计划；生产写操作需在审批通过后进入安全变更流程。",
        ]
    )


def _render_change_executions(report: DiagnosisReport) -> str:
    executions = [item for item in report.change_executions if isinstance(item, dict)]
    if not executions:
        return "- 暂无安全变更执行记录。"

    lines: list[str] = []
    for item in executions[-5:]:
        raw_stages = item.get("stages")
        stages = raw_stages if isinstance(raw_stages, list) else build_change_execution_stages(item)
        lines.extend(
            [
                f"- 执行ID：{item.get('change_execution_id') or '未记录'}",
                f"  - 状态：{item.get('status') or 'unknown'}；模式：{item.get('mode') or 'unknown'}",
                f"  - 审批ID：{item.get('approval_id') or '未记录'}；计划ID：{item.get('change_plan_id') or '未记录'}",
            ]
        )
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            label = stage.get("label") or stage.get("key") or "stage"
            status = stage.get("status") or "未执行"
            reason = stage.get("reason") or "未记录"
            lines.append(f"  - {label}：{status}；{reason}")
    return "\n".join(lines)


def _render_inline_list(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "无"
    return ", ".join(str(item) for item in value)


def _render_indented_bullets(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "  - 无"
    return "\n".join(f"  - {item}" for item in value)


def _render_tool_calls(tool_calls: list[dict[str, Any]]) -> str:
    if not tool_calls:
        return "- 无"
    lines = []
    for call in tool_calls:
        lines.append(
            "- "
            f"{call.get('tool_name', 'unknown')} "
            f"step={call.get('step_id', '')} "
            f"source={call.get('data_source', 'unknown')} "
            f"status={call.get('status', 'unknown')} "
            f"latency_ms={call.get('latency_ms', 0)} "
            f"input={call.get('input_summary') or '未记录'} "
            f"summary={call.get('output_summary') or call.get('error_message') or '未记录'}"
        )
    return "\n".join(lines)


def _build_dependency_signals(
    evidence: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect Jaeger/Tempo and Redpanda/Kafka evidence for UI/report display."""
    evidence_by_step_tool = {
        (str(item.get("step_id") or ""), str(item.get("source_tool") or "")): item
        for item in evidence
        if str(item.get("evidence_type") or "") in {"trace", "message_queue"}
    }
    signals: list[dict[str, Any]] = []
    for call in tool_calls:
        tool_name = str(call.get("tool_name") or "")
        if tool_name not in {"query_traces", "query_message_queue_status"}:
            continue
        evidence_item = evidence_by_step_tool.get((str(call.get("step_id") or ""), tool_name), {})
        signals.append(
            {
                "step_id": call.get("step_id", ""),
                "tool_name": tool_name,
                "domain": "tracing" if tool_name == "query_traces" else "message_queue",
                "backend": _dependency_backend(call, evidence_item),
                "status": call.get("status", "unknown"),
                "data_source": call.get("data_source", "unknown"),
                "latency_ms": call.get("latency_ms", 0.0),
                "summary": call.get("output_summary")
                or evidence_item.get("summary")
                or call.get("error_message")
                or "",
                "stance": evidence_item.get("stance", "neutral"),
                "confidence": evidence_item.get("confidence", 0.0),
                "confidence_reason": evidence_item.get("confidence_reason", ""),
                "fact": evidence_item.get("fact", ""),
                "next_step": evidence_item.get("next_step", ""),
                "input_summary": call.get("input_summary", ""),
            }
        )
    return signals


def _dependency_backend(call: dict[str, Any], evidence_item: dict[str, Any]) -> str:
    data_source = str(call.get("data_source") or evidence_item.get("data_source") or "")
    if data_source in {"jaeger", "tempo", "redpanda"}:
        return data_source
    tool_name = str(call.get("tool_name") or "")
    if tool_name == "query_traces":
        return "jaeger/tempo"
    if tool_name == "query_message_queue_status":
        return "redpanda/kafka"
    return data_source or "unknown"


def _render_dependency_signals(signals: list[dict[str, Any]]) -> str:
    if not signals:
        return "- 无"
    lines: list[str] = []
    for item in signals:
        lines.append(
            "- "
            f"{item.get('domain', 'dependency')} "
            f"backend={item.get('backend', 'unknown')} "
            f"tool={item.get('tool_name', 'unknown')} "
            f"step={item.get('step_id', '')} "
            f"source={item.get('data_source', 'unknown')} "
            f"status={item.get('status', 'unknown')} "
            f"stance={item.get('stance', 'neutral')} "
            f"confidence={float(item.get('confidence') or 0.0):.2f} "
            f"summary={item.get('summary') or '未记录'}"
        )
    return "\n".join(lines)


def _render_approval_decision(report: DiagnosisReport) -> str:
    approval = report.approval_decision or report.risk_summary.get("approval_decision") or {}
    if not approval:
        return "- 审批详情：无"

    lines = [
        f"- 审批动作：{approval.get('action') or '未记录'}",
        f"- 审批ID：{approval.get('approval_id') or '未记录'}",
        f"- 审批人：{approval.get('decided_by') or '未处理'}",
        f"- 审批结果：{approval.get('status') or report.approval_status}",
        f"- 审批时间：{approval.get('decided_at') or '未处理'}",
        f"- 审批原因：{approval.get('decision_reason') or approval.get('reason') or '未填写'}",
    ]
    if approval.get("created_at"):
        lines.append(f"- 提交审批时间：{approval.get('created_at')}")
    if approval.get("tool_name"):
        lines.append(f"- 关联工具：{approval.get('tool_name')}")
    return "\n".join(lines)


def _render_evidence_quality(report: DiagnosisReport) -> str:
    profile = report.evidence_profile or {}
    by_type = _as_dict(profile.get("by_type"))
    by_stance = _as_dict(profile.get("by_stance"))
    lines = [
        f"- 类型分布：{_render_counter(by_type)}",
        f"- 立场分布：{_render_counter(by_stance)}",
    ]
    for item in report.evidence[:8]:
        lines.append(
            "- "
            f"{item.get('source_tool', 'unknown')} "
            f"source={item.get('data_source', 'unknown')} "
            f"type={item.get('evidence_type', 'unknown')} "
            f"stance={item.get('stance', 'neutral')} "
            f"confidence={float(item.get('confidence', 0.0)):.2f} "
            f"reason={item.get('confidence_reason', '') or '未标注'}"
        )
    return "\n".join(lines)


def _render_manual_action_boundary(report: DiagnosisReport) -> str:
    if report.manual_action_required:
        return "\n".join(
            [
                "- Agent 只输出诊断和处置建议；不直接执行生产写操作。",
                "- 人工执行前需要确认审批、影响范围、观察窗口和回滚方案。",
                "- 若变更后指标或日志未恢复，应立即回滚并升级人工排查。",
            ]
        )
    return "\n".join(
        [
            "- 当前报告未要求自动变更。",
            "- 如需执行重启、扩容、SQL 或配置修改，必须重新进入审批或变更流程。",
        ]
    )


def _render_counter(counter: dict[str, Any]) -> str:
    if not counter:
        return "无"
    return ", ".join(f"{key}={value}" for key, value in sorted(counter.items()))


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _render_runbook_references(evidence: list[dict[str, Any]]) -> str:
    references = _extract_runbook_references(evidence)
    if not references:
        return "- 无"

    lines = []
    for item in references[:8]:
        score = item.get("score")
        score_text = "未知" if score is None else f"{float(score):.4f}"
        heading = str(item.get("heading_path") or "未标注章节")
        lines.append(
            "- "
            f"{item.get('source_file', '未知来源')} "
            f"chunk={item.get('chunk_id', 'unknown')} "
            f"score={score_text} "
            f"heading={heading}"
        )
    return "\n".join(lines)


def _extract_runbook_references(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in evidence:
        payloads = _candidate_retrieval_payloads(item)
        for payload in payloads:
            for result in payload.get("retrieval_results", []) or []:
                if not isinstance(result, dict):
                    continue
                key = str(result.get("chunk_id") or result.get("source_file") or result)
                if key in seen:
                    continue
                seen.add(key)
                references.append(result)
    return references


def _candidate_retrieval_payloads(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    raw_data = _as_dict(evidence.get("raw_data"))
    output = _as_dict(raw_data.get("output"))
    payloads = []
    if raw_data.get("retrieval_results"):
        payloads.append(raw_data)
    if output.get("retrieval_results"):
        payloads.append(output)
    return payloads


def _record_report_generated(
    report: DiagnosisReport,
    *,
    existing_events: list[TraceEvent],
) -> None:
    """Append report persistence to an existing trace stream when one exists."""
    if not report.trace_id or not existing_events:
        return
    if any(
        event.event_type == "report_generated"
        and event.metadata.get("report_id") == report.report_id
        for event in existing_events
    ):
        return
    trace_service.create_event(
        trace_id=report.trace_id,
        incident_id=report.incident_id,
        node_name="report_generator",
        event_type="report_generated",
        output_summary=f"report_id={report.report_id}, status={report.status}",
        status=report.status,
        metadata={
            "report_id": report.report_id,
            "approval_status": report.approval_status,
            "manual_action_required": report.manual_action_required,
        },
    )


report_generator = ReportGenerator()
