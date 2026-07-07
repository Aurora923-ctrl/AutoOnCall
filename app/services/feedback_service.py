"""Feedback persistence, bad-case classification, and eval-case drafting."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.config import config
from app.models.feedback import (
    BAD_CASE_CATEGORY_LABELS,
    BadCaseCategory,
    BadCaseFeedback,
    BadCaseFeedbackCreate,
    DiagnosisFeedback,
    DiagnosisFeedbackCreate,
    FeedbackEvidence,
)
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent

HIGH_VALUE_CATEGORIES: set[BadCaseCategory] = {
    "retrieval_failure",
    "missing_citation",
    "tool_failure",
    "hallucination_risk",
    "permission_denied",
    "poor_report_quality",
}


class FeedbackService:
    """Persist feedback and classify it into improvement/eval backlog buckets."""

    def __init__(self, storage_path: str | Path | None = None):
        self.storage_path = Path(storage_path or config.aiops_feedback_path)

    def submit_feedback(
        self,
        *,
        incident_id: str,
        payload: DiagnosisFeedbackCreate,
        report: DiagnosisReport | None = None,
        trace_events: list[TraceEvent] | None = None,
    ) -> DiagnosisFeedback:
        feedback = DiagnosisFeedback(
            incident_id=incident_id,
            report_id=payload.report_id,
            root_cause_correct=payload.root_cause_correct,
            accepted_suggestion=payload.accepted_suggestion,
            operator_note=payload.operator_note.strip(),
            improvement_items=classify_improvement_items(payload),
        )
        self._append_json("diagnosis", feedback.model_dump(mode="json"))

        bad_case_payload = bad_case_payload_from_diagnosis_feedback(
            incident_id=incident_id,
            payload=payload,
            report=report,
            trace_events=trace_events or [],
        )
        if bad_case_payload is not None:
            bad_case = build_bad_case_feedback(bad_case_payload)
            bad_case.evidence.metadata.setdefault("incident_id", incident_id)
            bad_case.evidence.metadata.setdefault("report_id", payload.report_id)
            self._append_json("bad_case", bad_case.model_dump(mode="json"))
        return feedback

    def submit_bad_case_feedback(self, payload: BadCaseFeedbackCreate) -> BadCaseFeedback:
        """Persist direct thumb feedback from RAG chat or AIOps clients."""
        feedback = build_bad_case_feedback(payload)
        self._append_json("bad_case", feedback.model_dump(mode="json"))
        return feedback

    def list_feedback(self, *, incident_id: str | None = None) -> list[DiagnosisFeedback]:
        items = [
            DiagnosisFeedback.model_validate(record["payload"])
            for record in _read_records(self.storage_path)
            if record.get("record_type") == "diagnosis"
        ]
        if incident_id:
            items = [item for item in items if item.incident_id == incident_id]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def list_bad_cases(
        self,
        *,
        target: str | None = None,
        high_value_only: bool = False,
    ) -> list[BadCaseFeedback]:
        items = [
            BadCaseFeedback.model_validate(record["payload"])
            for record in _read_records(self.storage_path)
            if record.get("record_type") == "bad_case"
        ]
        if target:
            items = [item for item in items if item.target == target]
        if high_value_only:
            items = [item for item in items if item.high_value]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def _append_json(self, record_type: str, payload: dict[str, Any]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"record_type": record_type, "payload": payload}
        with self.storage_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_bad_case_feedback(payload: BadCaseFeedbackCreate) -> BadCaseFeedback:
    """Classify and normalize direct RAG/AIOps bad-case feedback."""
    category = payload.category or infer_bad_case_category(
        target=payload.target,
        vote=payload.vote,
        reason=payload.reason,
        evidence=FeedbackEvidence(
            query=payload.query,
            answer=payload.answer,
            citations=payload.citations,
            retrieval_results=payload.retrieval_results,
            rejected_results=payload.rejected_results,
            trace_id=payload.trace_id,
            tool_calls=payload.tool_calls,
            metadata=payload.metadata,
        ),
    )
    evidence = FeedbackEvidence(
        query=payload.query,
        answer=payload.answer,
        citations=payload.citations,
        retrieval_results=payload.retrieval_results,
        rejected_results=payload.rejected_results,
        trace_id=payload.trace_id,
        tool_calls=payload.tool_calls,
        metadata=payload.metadata,
    )
    high_value = is_high_value_bad_case(
        vote=payload.vote,
        expected_answer=payload.expected_answer,
        category=category,
        evidence=evidence,
    )
    return BadCaseFeedback(
        target=payload.target,
        vote=payload.vote,
        category=category,
        category_label=BAD_CASE_CATEGORY_LABELS[category],
        reason=payload.reason,
        expected_answer=payload.expected_answer,
        evidence=evidence,
        high_value=high_value,
        improvement_items=classify_bad_case_improvement_items(category, payload.reason),
    )


def classify_improvement_items(payload: DiagnosisFeedbackCreate) -> list[dict[str, str]]:
    """Map low-scoring report feedback to concrete backlog categories."""
    note = payload.operator_note.strip()
    items: list[dict[str, str]] = []
    if payload.root_cause_correct in {"partial", "no"}:
        items.append(
            {
                "type": "eval_case_draft",
                "reason": "根因反馈不是 yes，需要沉淀为诊断回归样例。",
                "detail": note,
            }
        )
        items.append(
            {
                "type": "tool_gap",
                "reason": "根因证据不足或证据域缺失，需要检查工具覆盖。",
                "detail": note,
            }
        )
    if payload.accepted_suggestion == "no":
        items.append(
            {
                "type": "report_template_issue",
                "reason": "处置建议未被采纳，需要改进报告模板或风险边界表达。",
                "detail": note,
            }
        )
    if _looks_like_runbook_gap(note):
        items.append(
            {
                "type": "rag_doc_gap",
                "reason": "反馈提到 Runbook、文档、SOP 或知识缺口。",
                "detail": note,
            }
        )
    return items or [
        {
            "type": "accepted_case",
            "reason": "根因和建议均被采纳，可作为正向样例保留。",
            "detail": note,
        }
    ]


def classify_bad_case_improvement_items(
    category: BadCaseCategory, reason: str
) -> list[dict[str, str]]:
    """Convert bad-case category into concrete engineering backlog items."""
    mapping: dict[BadCaseCategory, tuple[str, str]] = {
        "retrieval_failure": ("rag_eval_case", "补充 RAG 召回回归 case，并检查分块/关键词/重排。"),
        "missing_citation": (
            "citation_guard_case",
            "补充引用完整性 case，要求 source_file + chunk_id。",
        ),
        "tool_failure": ("aiops_eval_case", "补充工具失败降级 case，验证 trace 和报告兜底。"),
        "hallucination_risk": ("grounding_case", "补充拒答/事实边界 case，避免脱离证据强答。"),
        "permission_denied": (
            "permission_case",
            "补充权限/危险动作阻断 case，验证 forbidden 边界。",
        ),
        "poor_report_quality": (
            "report_quality_case",
            "补充报告质量 case，验证根因、证据和建议可读性。",
        ),
    }
    item_type, item_reason = mapping[category]
    return [{"type": item_type, "reason": item_reason, "detail": reason.strip()}]


def bad_case_payload_from_diagnosis_feedback(
    *,
    incident_id: str,
    payload: DiagnosisFeedbackCreate,
    report: DiagnosisReport | None,
    trace_events: list[TraceEvent],
) -> BadCaseFeedbackCreate | None:
    """Build an AIOps bad-case payload from report feedback and trace/report context."""
    if (
        payload.vote != "thumb_down"
        and payload.root_cause_correct == "yes"
        and payload.accepted_suggestion == "yes"
    ):
        return None
    tool_calls = _tool_calls_from_report_and_trace(report, trace_events)
    query = ""
    if report is not None:
        query = report.summary or report.title or incident_id
    else:
        query = payload.operator_note or incident_id
    answer = report.markdown if report is not None else ""
    category = payload.category or infer_aiops_category_from_feedback(payload, tool_calls)
    return BadCaseFeedbackCreate(
        target="aiops",
        vote=payload.vote,
        reason=payload.operator_note,
        expected_answer=payload.expected_answer,
        category=category,
        query=query,
        answer=answer,
        trace_id=report.trace_id if report is not None else "",
        tool_calls=tool_calls,
        metadata={
            "incident_id": incident_id,
            "report_id": payload.report_id,
            "root_cause_correct": payload.root_cause_correct,
            "accepted_suggestion": payload.accepted_suggestion,
            "service_name": report.service_name if report is not None else "",
            "severity": report.severity if report is not None else "",
            "environment": report.environment if report is not None else "",
        },
    )


def infer_aiops_category_from_feedback(
    payload: DiagnosisFeedbackCreate, tool_calls: list[dict[str, Any]]
) -> BadCaseCategory:
    """Infer the best bad-case bucket for report feedback."""
    note = payload.operator_note.lower()
    if any(_tool_failed(call) for call in tool_calls) or _contains_any(note, ["tool", "工具失败"]):
        return "tool_failure"
    if _contains_any(note, ["权限", "审批", "forbidden", "permission", "拒绝"]):
        return "permission_denied"
    if payload.root_cause_correct in {"partial", "no"}:
        return "hallucination_risk"
    return "poor_report_quality"


def infer_bad_case_category(
    *,
    target: str,
    vote: str,
    reason: str,
    evidence: FeedbackEvidence,
) -> BadCaseCategory:
    """Infer bad-case category from feedback text and runtime evidence."""
    text = f"{reason}\n{evidence.answer}".lower()
    if target == "rag":
        if not evidence.citations or _contains_any(text, ["引用", "citation", "source_file"]):
            return "missing_citation"
        if evidence.rejected_results or _contains_any(text, ["召回", "retrieval", "没搜到"]):
            return "retrieval_failure"
        return "hallucination_risk" if vote == "thumb_down" else "retrieval_failure"
    if any(_tool_failed(call) for call in evidence.tool_calls):
        return "tool_failure"
    if _contains_any(text, ["权限", "审批", "forbidden", "permission", "拒绝"]):
        return "permission_denied"
    if _contains_any(text, ["报告", "建议", "格式", "可读"]):
        return "poor_report_quality"
    return "hallucination_risk"


def is_high_value_bad_case(
    *,
    vote: str,
    expected_answer: str,
    category: BadCaseCategory,
    evidence: FeedbackEvidence,
) -> bool:
    """Return True when feedback is actionable enough to enter eval regression."""
    if vote != "thumb_down":
        return False
    if category not in HIGH_VALUE_CATEGORIES:
        return False
    if expected_answer.strip():
        return True
    if category in {"hallucination_risk", "poor_report_quality"} and evidence.query.strip():
        return True
    if evidence.query.strip() and (
        evidence.retrieval_results
        or evidence.rejected_results
        or evidence.tool_calls
        or evidence.trace_id
    ):
        return True
    return False


def _tool_calls_from_report_and_trace(
    report: DiagnosisReport | None, trace_events: list[TraceEvent]
) -> list[dict[str, Any]]:
    if report is not None and report.tool_calls:
        return [dict(item) for item in report.tool_calls if isinstance(item, dict)]
    calls = []
    for event in trace_events:
        if event.event_type != "tool_call":
            continue
        calls.append(
            {
                "trace_id": event.trace_id,
                "incident_id": event.incident_id,
                "tool_name": event.tool_name or event.node_name,
                "input_args": event.tool_args,
                "output": event.tool_result,
                "latency_ms": event.latency_ms,
                "status": event.status,
                "error_message": event.error_message,
                "data_source": event.metadata.get("data_source", "unknown"),
            }
        )
    return calls


def _looks_like_runbook_gap(note: str) -> bool:
    lowered = note.lower()
    return any(token in lowered for token in ["runbook", "doc", "sop", "文档", "手册", "知识"])


def _contains_any(text: str, tokens: list[str]) -> bool:
    return any(token.lower() in text for token in tokens)


def _tool_failed(call: dict[str, Any]) -> bool:
    return str(call.get("status") or "").lower() in {"failed", "error", "timeout"}


def _read_records(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict) and "record_type" in raw and "payload" in raw:
            items.append(raw)
        elif isinstance(raw, dict) and {"incident_id", "report_id"}.issubset(raw):
            items.append({"record_type": "diagnosis", "payload": raw})
        elif isinstance(raw, dict) and {"target", "vote", "evidence"}.issubset(raw):
            items.append({"record_type": "bad_case", "payload": raw})
    return items


def stable_eval_case_id(prefix: str, source: str) -> str:
    """Build a readable deterministic eval-case id from feedback content."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", source.lower()).strip("_")
    if not slug:
        slug = "case"
    return f"{prefix}_{slug[:48]}"


feedback_service = FeedbackService()
