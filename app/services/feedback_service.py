"""Feedback persistence, bad-case classification, and eval-case drafting."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any, TypeVar, cast

from filelock import FileLock

from app.config import config
from app.models.feedback import (
    BAD_CASE_CATEGORY_LABELS,
    BadCaseCategory,
    BadCaseFeedback,
    BadCaseFeedbackCreate,
    DiagnosisFeedback,
    DiagnosisFeedbackCreate,
    EvalBacklogItem,
    EvalBacklogPriority,
    FeedbackEvidence,
)
from app.models.incident import utc_now
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.utils.redaction import redact_sensitive_data
from app.utils.structured_data import as_dict

FeedbackItem = TypeVar("FeedbackItem", DiagnosisFeedback, BadCaseFeedback)

HIGH_VALUE_CATEGORIES: set[BadCaseCategory] = {
    "retrieval_failure",
    "missing_citation",
    "tool_failure",
    "hallucination_risk",
    "permission_denied",
    "poor_report_quality",
}
MAX_BAD_CASE_ANSWER_CHARS = 12_000


class FeedbackService:
    """Persist feedback and classify it into improvement/eval backlog buckets."""

    def __init__(self, storage_path: str | Path | None = None):
        self.storage_path = Path(storage_path or config.aiops_feedback_path)
        self.lock_path = self.storage_path.with_suffix(f"{self.storage_path.suffix}.lock")
        self._thread_lock = threading.RLock()

    def submit_feedback(
        self,
        *,
        incident_id: str,
        payload: DiagnosisFeedbackCreate,
        report: DiagnosisReport | None = None,
        trace_events: list[TraceEvent] | None = None,
        owner_id: str = "anonymous",
    ) -> DiagnosisFeedback:
        payload = normalize_diagnosis_feedback_vote(payload)
        owner_id = normalize_feedback_owner(owner_id)
        dedupe_fingerprint = diagnosis_feedback_dedupe_fingerprint(
            owner_id,
            incident_id,
            payload.report_id,
        )
        feedback = DiagnosisFeedback(
            owner_id=owner_id,
            dedupe_fingerprint=dedupe_fingerprint,
            incident_id=incident_id,
            report_id=payload.report_id,
            run_id=payload.run_id,
            session_id=payload.session_id,
            trace_id=payload.trace_id or (report.trace_id if report is not None else ""),
            vote=payload.vote,
            root_cause_correct=payload.root_cause_correct,
            accepted_suggestion=payload.accepted_suggestion,
            operator_note=payload.operator_note.strip(),
            improvement_items=classify_improvement_items(payload),
        )
        bad_case_payload = bad_case_payload_from_diagnosis_feedback(
            incident_id=incident_id,
            payload=payload,
            report=report,
            trace_events=trace_events or [],
        )
        related_records: list[tuple[str, dict[str, Any]]] = []
        if bad_case_payload is not None:
            bad_case = build_bad_case_feedback(
                bad_case_payload,
                owner_id=owner_id,
                dedupe_fingerprint=f"{dedupe_fingerprint}:bad_case",
            )
            if bad_case.high_value:
                bad_case.evidence.metadata.setdefault("incident_id", incident_id)
                bad_case.evidence.metadata.setdefault("report_id", payload.report_id)
                backlog_item = build_eval_backlog_item(bad_case, source="diagnosis_feedback")
                related_records = [
                    ("bad_case", bad_case.model_dump(mode="json")),
                    ("eval_backlog", backlog_item.model_dump(mode="json")),
                ]
        return cast(
            DiagnosisFeedback,
            self._upsert_feedback_bundle(
                primary_type="diagnosis",
                primary=feedback,
                related_records=related_records,
            ),
        )

    def submit_bad_case_feedback(
        self,
        payload: BadCaseFeedbackCreate,
        *,
        owner_id: str = "anonymous",
        reference_store: Any | None = None,
    ) -> BadCaseFeedback:
        """Persist direct thumb feedback from RAG chat or AIOps clients."""
        owner_id = normalize_feedback_owner(owner_id)
        dedupe_fingerprint = direct_feedback_dedupe_fingerprint(owner_id, payload)
        feedback = build_bad_case_feedback(
            payload,
            owner_id=owner_id,
            dedupe_fingerprint=dedupe_fingerprint,
        )
        if reference_store is not None:
            feedback = self.verify_feedback_references(feedback, store=reference_store)
        related_records: list[tuple[str, dict[str, Any]]] = []
        if feedback.high_value and feedback.reference_status != "orphaned":
            backlog_item = build_eval_backlog_item(feedback, source="direct_feedback")
            related_records.append(("eval_backlog", backlog_item.model_dump(mode="json")))
        return cast(
            BadCaseFeedback,
            self._upsert_feedback_bundle(
                primary_type="bad_case",
                primary=feedback,
                related_records=related_records,
            ),
        )

    def verify_feedback_references(
        self,
        item: BadCaseFeedback,
        *,
        store: Any | None = None,
    ) -> BadCaseFeedback:
        """Mark runtime links as verified or orphaned without deleting feedback."""
        metadata = item.evidence.metadata or {}
        incident_id = item.incident_id or str(metadata.get("incident_id") or "")
        report_id = item.report_id or str(metadata.get("report_id") or "")
        session_id = item.session_id or str(metadata.get("session_id") or "")
        trace_id = item.trace_id or item.evidence.trace_id
        reasons: list[str] = []
        if not incident_id and any((report_id, session_id)):
            reasons.append("missing_incident_id")
        if store is not None:
            if incident_id and store.get_incident_state(incident_id) is None:
                reasons.append("incident_not_found")
            if report_id and not _store_has_report(store, incident_id, report_id):
                reasons.append("report_not_found")
            if session_id and not _store_has_session(store, incident_id, session_id):
                reasons.append("session_not_found")
            if item.run_id and session_id and item.run_id != session_id:
                reasons.append("run_session_mismatch")
            if item.run_id and not session_id:
                reasons.append("run_without_session")
            if incident_id and trace_id and not _store_has_trace(store, incident_id, trace_id):
                reasons.append("trace_not_found")
        status = "orphaned" if reasons else "verified" if store is not None else "unverified"
        return item.model_copy(
            update={
                "incident_id": incident_id,
                "report_id": report_id,
                "run_id": item.run_id or str(metadata.get("run_id") or ""),
                "session_id": session_id,
                "trace_id": trace_id,
                "reference_status": status,
                "orphan_reasons": dedupe_strings(reasons),
            }
        )

    def list_feedback(
        self,
        *,
        incident_id: str | None = None,
        owner_id: str | None = None,
    ) -> list[DiagnosisFeedback]:
        items = [
            DiagnosisFeedback.model_validate(record["payload"])
            for record in _read_records(self.storage_path)
            if record.get("record_type") == "diagnosis"
        ]
        if incident_id:
            items = [item for item in items if item.incident_id == incident_id]
        if owner_id:
            items = [item for item in items if item.owner_id == owner_id]
        return _latest_feedback_items(items)

    def list_bad_cases(
        self,
        *,
        target: str | None = None,
        high_value_only: bool = False,
        owner_id: str | None = None,
        reference_store: Any | None = None,
    ) -> list[BadCaseFeedback]:
        items = [
            BadCaseFeedback.model_validate(record["payload"])
            for record in _read_records(self.storage_path)
            if record.get("record_type") == "bad_case"
        ]
        if reference_store is not None:
            items = [self.verify_feedback_references(item, store=reference_store) for item in items]
            self._persist_reference_updates(items)
            self._reject_orphaned_backlog(items)
        if target:
            items = [item for item in items if item.target == target]
        if high_value_only:
            items = [item for item in items if item.high_value]
        if owner_id:
            items = [item for item in items if item.owner_id == owner_id]
        return _latest_feedback_items(items)

    def _persist_reference_updates(self, items: list[BadCaseFeedback]) -> None:
        """Persist orphan/verified status discovered while reading runtime data."""
        updates = {
            item.feedback_id: sanitize_feedback_payload(item.model_dump(mode="json"))
            for item in items
        }
        if not updates:
            return
        with self._storage_lock():
            raw_lines = _read_raw_lines(self.storage_path)
            changed = False
            rewritten: list[str] = []
            for line in raw_lines:
                record = _parse_record_line(line)
                if record is None:
                    rewritten.append(line)
                    continue
                if record.get("record_type") != "bad_case":
                    rewritten.append(line)
                    continue
                payload = record.get("payload")
                feedback_id = (
                    str(payload.get("feedback_id") or "") if isinstance(payload, dict) else ""
                )
                if feedback_id in updates and payload != updates[feedback_id]:
                    record["payload"] = updates[feedback_id]
                    changed = True
                rewritten.append(json.dumps(record, ensure_ascii=False, default=str))
            if changed:
                self._write_raw_lines(rewritten)

    def _reject_orphaned_backlog(self, items: list[BadCaseFeedback]) -> None:
        orphaned_ids = {item.feedback_id for item in items if item.reference_status == "orphaned"}
        if not orphaned_ids:
            return
        with self._storage_lock():
            raw_lines = _read_raw_lines(self.storage_path)
            rewritten: list[str] = []
            changed = False
            now = utc_now().isoformat()
            for line in raw_lines:
                record = _parse_record_line(line)
                if record is None:
                    rewritten.append(line)
                    continue
                payload = record.get("payload")
                if (
                    record.get("record_type") == "eval_backlog"
                    and isinstance(payload, dict)
                    and str(payload.get("feedback_id") or "") in orphaned_ids
                    and payload.get("review_status") != "rejected"
                ):
                    payload["review_status"] = "rejected"
                    payload["reviewed_by"] = "reference_integrity"
                    payload["reviewed_at"] = now
                    payload["updated_at"] = now
                    metadata = payload.get("metadata")
                    metadata = dict(metadata) if isinstance(metadata, dict) else {}
                    metadata["rejection_reason"] = "orphaned_feedback_reference"
                    payload["metadata"] = metadata
                    changed = True
                rewritten.append(json.dumps(record, ensure_ascii=False, default=str))
            if changed:
                self._write_raw_lines(rewritten)

    def list_eval_backlog(
        self,
        *,
        target: str | None = None,
        review_status: str | None = None,
        owner_id: str | None = None,
        reference_store: Any | None = None,
    ) -> list[EvalBacklogItem]:
        """List reviewable bad-case drafts before they are promoted to eval YAML."""
        owner_feedback_ids: set[str] | None = None
        if reference_store is not None:
            refreshed_bad_cases = self.list_bad_cases(
                owner_id=owner_id,
                reference_store=reference_store,
            )
            if owner_id:
                owner_feedback_ids = {item.feedback_id for item in refreshed_bad_cases}
        items = [
            EvalBacklogItem.model_validate(record["payload"])
            for record in _read_records(self.storage_path)
            if record.get("record_type") == "eval_backlog"
        ]
        if target:
            items = [item for item in items if item.target == target]
        if review_status:
            items = [item for item in items if item.review_status == review_status]
        if owner_id:
            if owner_feedback_ids is None:
                owner_feedback_ids = {
                    item.feedback_id for item in self.list_bad_cases(owner_id=owner_id)
                }
            items = [item for item in items if item.feedback_id in owner_feedback_ids]
        return _latest_backlog_items(items)

    def _append_json(self, record_type: str, payload: dict[str, Any]) -> None:
        with self._storage_lock():
            records = list(_read_records(self.storage_path))
            records.append(
                {"record_type": record_type, "payload": sanitize_feedback_payload(payload)}
            )
            self._write_records(records)

    def _upsert_feedback_bundle(
        self,
        *,
        primary_type: str,
        primary: DiagnosisFeedback | BadCaseFeedback,
        related_records: list[tuple[str, dict[str, Any]]],
    ) -> DiagnosisFeedback | BadCaseFeedback:
        with self._storage_lock():
            records = list(_read_records(self.storage_path))
            primary_payload = primary.model_dump(mode="json")
            existing_index = _find_feedback_record_index(
                records,
                record_type=primary_type,
                dedupe_fingerprint=primary.dedupe_fingerprint,
            )
            if existing_index is not None:
                existing_payload = records[existing_index]["payload"]
                primary_payload["feedback_id"] = existing_payload["feedback_id"]
                primary_payload["created_at"] = existing_payload.get(
                    "created_at",
                    primary_payload["created_at"],
                )
                comparable_existing = dict(existing_payload)
                comparable_new = sanitize_feedback_payload(primary_payload)
                for value in (comparable_existing, comparable_new):
                    value.pop("updated_at", None)
                if comparable_existing == comparable_new:
                    return type(primary).model_validate(existing_payload)
            primary_payload["updated_at"] = utc_now().isoformat()
            sanitized_primary = sanitize_feedback_payload(primary_payload)
            if existing_index is None:
                records.append({"record_type": primary_type, "payload": sanitized_primary})
            else:
                records[existing_index] = {
                    "record_type": primary_type,
                    "payload": sanitized_primary,
                }

            primary_model = type(primary).model_validate(sanitized_primary)
            _remove_related_records(
                records,
                primary_model.feedback_id,
                include_bad_case=primary_type == "diagnosis",
            )
            for record_type, payload in related_records:
                payload["feedback_id"] = primary_model.feedback_id
                if record_type == "eval_backlog":
                    payload["backlog_id"] = stable_eval_case_id("ebl", primary_model.feedback_id)
                    payload["suggested_eval_case_id"] = stable_eval_case_id(
                        f"draft_{payload.get('target') or 'aiops'}",
                        f"{primary_model.feedback_id}_{_backlog_query(payload, primary_model)}",
                    )
                records.append(
                    {
                        "record_type": record_type,
                        "payload": sanitize_feedback_payload(payload),
                    }
                )
            self._write_records(records)
            return primary_model

    def _storage_lock(self) -> _FeedbackStorageLock:
        return _FeedbackStorageLock(self._thread_lock, FileLock(str(self.lock_path)))

    def _write_records(self, records: list[dict[str, Any]]) -> None:
        self._write_raw_lines(
            [json.dumps(record, ensure_ascii=False, default=str) for record in records]
        )

    def _write_raw_lines(self, lines: list[str]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.storage_path.with_suffix(f"{self.storage_path.suffix}.tmp")
        temp_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        temp_path.replace(self.storage_path)


def build_bad_case_feedback(
    payload: BadCaseFeedbackCreate,
    *,
    owner_id: str = "anonymous",
    dedupe_fingerprint: str = "",
) -> BadCaseFeedback:
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
        owner_id=normalize_feedback_owner(owner_id),
        dedupe_fingerprint=dedupe_fingerprint
        or direct_feedback_dedupe_fingerprint(owner_id, payload),
        incident_id=str(payload.metadata.get("incident_id") or ""),
        report_id=str(payload.metadata.get("report_id") or ""),
        run_id=str(payload.metadata.get("run_id") or ""),
        session_id=str(payload.metadata.get("session_id") or ""),
        trace_id=payload.trace_id,
        target=payload.target,
        vote=payload.vote,
        category=category,
        category_label=BAD_CASE_CATEGORY_LABELS[category],
        reason=payload.reason,
        expected_answer=payload.expected_answer,
        evidence=evidence,
        high_value=high_value,
        reference_status="unverified",
        improvement_items=classify_bad_case_improvement_items(category, payload.reason),
    )


def normalize_diagnosis_feedback_vote(payload: DiagnosisFeedbackCreate) -> DiagnosisFeedbackCreate:
    """Treat fully accepted report feedback as positive unless it carries a clear failure signal."""
    if (
        payload.vote == "thumb_down"
        and payload.root_cause_correct == "yes"
        and payload.accepted_suggestion == "yes"
        and not payload.expected_answer.strip()
        and payload.category is None
    ):
        positive_tokens = ["没有问题", "无问题", "没问题", "都已采纳", "符合预期", "正确"]
        if any(token in payload.operator_note for token in positive_tokens):
            return payload.model_copy(update={"vote": "thumb_up"})
        negative_tokens = ["错误", "不对", "缺少", "失败", "bad", "wrong", "missing"]
        note = payload.operator_note.lower()
        if not any(token in note for token in negative_tokens):
            return payload.model_copy(update={"vote": "thumb_up"})
    return payload


def build_eval_backlog_item(
    item: BadCaseFeedback,
    *,
    source: str = "feedback",
) -> EvalBacklogItem:
    """Build a reviewable eval-backlog draft from one high-value bad case."""
    query = item.evidence.query.strip() or item.reason.strip() or item.feedback_id
    target_prefix = str(item.target or "aiops")
    suggested_case_id = stable_eval_case_id(
        f"draft_{target_prefix}",
        f"{item.feedback_id}_{query}",
    )
    suggested_file, suggested_suite = suggested_eval_destination(item)
    return EvalBacklogItem(
        backlog_id=stable_eval_case_id("ebl", item.feedback_id),
        feedback_id=item.feedback_id,
        source=source,
        target=item.target,
        category=item.category,
        priority=_backlog_priority(item),
        review_status="new",
        suggested_eval_file=suggested_file,
        suggested_eval_suite=suggested_suite,
        suggested_eval_case_id=suggested_case_id,
        suggested_eval_dimension=_suggested_eval_dimension(item),
        expected_behavior=_expected_behavior(item),
        failure_reasons=_backlog_failure_reasons(item),
        evidence_snapshot=_evidence_snapshot(item),
        links=_backlog_links(item),
        metadata={
            "category_label": item.category_label,
            "high_value": item.high_value,
            "vote": item.vote,
            "created_from_feedback_at": item.created_at.isoformat(),
            "quality_boundary": _quality_boundary_for_suite(suggested_suite),
            "promotion_policy": _promotion_policy_for_suite(suggested_suite),
        },
    )


def summarize_eval_backlog(items: list[EvalBacklogItem]) -> dict[str, Any]:
    """Summarize eval-backlog drafts for API and export reports."""
    by_target: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    by_review_status: dict[str, int] = {}
    by_eval_file: dict[str, int] = {}
    for item in items:
        by_target[item.target] = by_target.get(item.target, 0) + 1
        by_category[item.category] = by_category.get(item.category, 0) + 1
        by_priority[item.priority] = by_priority.get(item.priority, 0) + 1
        by_review_status[item.review_status] = by_review_status.get(item.review_status, 0) + 1
        by_eval_file[item.suggested_eval_file] = by_eval_file.get(item.suggested_eval_file, 0) + 1
    return {
        "total": len(items),
        "by_target": by_target,
        "by_category": by_category,
        "by_priority": by_priority,
        "by_review_status": by_review_status,
        "by_eval_file": by_eval_file,
    }


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
    if (
        not payload.expected_answer.strip()
        and payload.category is None
        and payload.accepted_suggestion == "yes"
        and not _strong_negative_feedback_note(payload.operator_note)
    ):
        return None
    tool_calls = _tool_calls_from_report_and_trace(report, trace_events)
    query = ""
    if report is not None:
        query = report.summary or report.title or incident_id
    else:
        query = payload.operator_note or incident_id
    answer = _truncate_bad_case_answer(report.markdown if report is not None else "")
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
            "run_id": payload.run_id,
            "session_id": payload.session_id,
            "trace_id": payload.trace_id or (report.trace_id if report is not None else ""),
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
    metric_text = _feedback_metric_text(evidence, text)
    if target == "ragas":
        if _contains_any(
            metric_text,
            [
                "id_based_context_recall",
                "id_based_context_precision",
                "citation_grounding_hit",
                "ragas_id_recall",
                "ragas_id_precision",
                "missing_citation",
            ],
        ):
            return "retrieval_failure"
        if _contains_any(metric_text, ["oncall_actionability_score", "ragas_actionability"]):
            return "poor_report_quality"
        if _contains_any(
            metric_text,
            [
                "answer_relevancy",
                "response_relevancy",
                "faithfulness",
                "incident_boundary_hit",
                "confusion_disambiguation_hit",
                "refusal_boundary",
                "refusal_boundary_hit",
            ],
        ):
            return "hallucination_risk"
        return "hallucination_risk"
    if target == "change":
        if _contains_any(
            metric_text,
            [
                "forbidden_change_block_rate",
                "forbidden_sql_blocked_rate",
                "approval_before_execute_rate",
                "approval_required_before_execution",
            ],
        ):
            return "permission_denied"
        if _contains_any(
            metric_text,
            [
                "precheck_recall",
                "rollback_recommendation_rate",
                "dry_run_before_execute_rate",
                "manual_record_required_rate",
                "change_plan_completeness",
            ],
        ):
            return "tool_failure"
        return "poor_report_quality"
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


def _feedback_metric_text(evidence: FeedbackEvidence, text: str) -> str:
    metadata = evidence.metadata or {}
    metric_values = [
        str(value)
        for key, value in metadata.items()
        if "metric" in str(key).lower() or "failed" in str(key).lower()
    ]
    return " ".join([text, *metric_values]).lower()


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
    if category in {"hallucination_risk", "poor_report_quality"} and not expected_answer.strip():
        if not evidence.trace_id and not evidence.tool_calls and not evidence.citations:
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


def _backlog_priority(item: BadCaseFeedback) -> EvalBacklogPriority:
    if item.category in {"permission_denied", "tool_failure", "hallucination_risk"}:
        return "P0"
    if item.category in {"retrieval_failure", "missing_citation", "poor_report_quality"}:
        return "P1"
    return "P2"


def _suggested_eval_file(item: BadCaseFeedback) -> str:
    return suggested_eval_destination(item)[0]


def suggested_eval_destination(item: BadCaseFeedback) -> tuple[str, str]:
    """Route one backlog draft to the suite it should enter after human review."""
    suite_hint = str(item.evidence.metadata.get("suite") or "").strip().lower()
    if item.target == "ragas" or suite_hint == "ragas":
        return "eval/ragas_cases.review.json", "ragas"
    if item.target == "change" or suite_hint == "change":
        return "eval/change_cases.yaml", "change"
    if item.target == "rag" or suite_hint == "rag":
        return "eval/rag_cases.yaml", "rag"
    return "eval/cases.yaml", "aiops"


def _suggested_eval_dimension(item: BadCaseFeedback) -> str:
    suggested_suite = suggested_eval_destination(item)[1]
    if suggested_suite == "ragas":
        return "ragas_answer_quality_gate"
    if suggested_suite == "change":
        return "safe_change_regression_gate"
    dimensions = {
        "retrieval_failure": "rag_recall_at_k",
        "missing_citation": "rag_citation_coverage",
        "tool_failure": "tool_failure_graceful_degradation",
        "hallucination_risk": "root_cause_grounding",
        "permission_denied": "forbidden_action_block",
        "poor_report_quality": "report_quality_gate",
    }
    return dimensions[item.category]


def _expected_behavior(item: BadCaseFeedback) -> str:
    if item.expected_answer.strip():
        return item.expected_answer.strip()
    suggested_suite = suggested_eval_destination(item)[1]
    if suggested_suite == "ragas":
        return (
            "RAGAS answer-quality regression should preserve context id grounding, citations, "
            "refusal boundaries, and OnCall actionability."
        )
    if suggested_suite == "change":
        return (
            "Safe change regression should preserve approval, dry-run, rollback, observation, "
            "and manual-record boundaries."
        )
    category_defaults = {
        "retrieval_failure": "RAG retrieval should return the expected runbook source.",
        "missing_citation": "The answer should include source_file and chunk_id citations.",
        "tool_failure": "The diagnosis should record the failed tool and produce a degraded report.",
        "hallucination_risk": "The diagnosis should avoid unsupported RCA and cite evidence.",
        "permission_denied": "Dangerous or unauthorized actions should be blocked before execution.",
        "poor_report_quality": "The report should explain evidence, uncertainty, and safe next steps.",
    }
    return category_defaults[item.category]


def _quality_boundary_for_suite(suite: str) -> str:
    boundaries = {
        "ragas": "RAGAS backlog drafts are answer-quality regression inputs, not live adapter facts.",
        "change": "Change backlog drafts are safe-change regression inputs, not production execution records.",
        "rag": "RAG backlog drafts are retrieval/citation regression inputs.",
        "aiops": "AIOps backlog drafts are diagnosis regression inputs.",
    }
    return boundaries.get(suite, boundaries["aiops"])


def _promotion_policy_for_suite(suite: str) -> str:
    if suite == "ragas":
        return "skip_rag_yaml; keep as reviewed RAGAS answer-quality fixture draft"
    return "requires_reviewed_backlog_before_yaml_promotion"


def _backlog_failure_reasons(item: BadCaseFeedback) -> list[str]:
    reasons = [item.reason.strip()] if item.reason.strip() else []
    reasons.extend(str(entry.get("reason") or "") for entry in item.improvement_items)
    if item.category == "tool_failure":
        failed_tools = [
            str(call.get("tool_name") or "unknown")
            for call in item.evidence.tool_calls
            if _tool_failed(call)
        ]
        if failed_tools:
            reasons.append("failed_tools=" + ",".join(dedupe_strings(failed_tools)))
    return dedupe_strings([reason for reason in reasons if reason])[:8]


def _evidence_snapshot(item: BadCaseFeedback) -> dict[str, Any]:
    metadata = dict(item.evidence.metadata or {})
    snapshot = {
        "query": item.evidence.query,
        "answer_preview": item.evidence.answer,
        "expected_answer": item.expected_answer,
        "citations": item.evidence.citations[:5],
        "retrieval_results": item.evidence.retrieval_results[:5],
        "rejected_results": item.evidence.rejected_results[:5],
        "trace_id": item.evidence.trace_id,
        "tool_calls": [_compact_tool_call(call) for call in item.evidence.tool_calls[:8]],
        "metadata": {
            key: metadata.get(key)
            for key in [
                "incident_id",
                "report_id",
                "session_id",
                "service_name",
                "severity",
                "environment",
                "root_cause_correct",
                "accepted_suggestion",
            ]
            if key in metadata
        },
    }
    return as_dict(
        redact_sensitive_data(
            snapshot,
            redact_auth_scheme=True,
            max_string_length=1200,
        )
    )


def _truncate_bad_case_answer(answer: str) -> str:
    """Keep feedback bad-case payloads bounded while the full report stays queryable."""
    suffix = "\n\n[truncated for eval backlog; full diagnosis report is available by report_id]"
    if len(answer) + len(suffix) <= MAX_BAD_CASE_ANSWER_CHARS:
        return answer + suffix
    return answer[: MAX_BAD_CASE_ANSWER_CHARS - len(suffix)] + suffix


def _strong_negative_feedback_note(note: str) -> bool:
    text = note.lower()
    return _contains_any(
        text,
        [
            "错误",
            "不对",
            "缺少",
            "失败",
            "没命中",
            "幻觉",
            "越权",
            "审批",
            "wrong",
            "missing",
            "failed",
            "hallucination",
        ],
    )


def _compact_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    output = as_dict(call.get("output"))
    return {
        "tool_name": call.get("tool_name") or call.get("name") or "",
        "status": call.get("status") or "",
        "error_message": call.get("error_message") or output.get("error_message") or "",
        "data_source": call.get("data_source") or output.get("source") or "",
        "artifact_id": call.get("artifact_id") or output.get("artifact_id") or "",
        "artifact_ref": call.get("artifact_ref") or output.get("artifact_ref") or "",
    }


def _backlog_links(item: BadCaseFeedback) -> dict[str, str]:
    metadata = item.evidence.metadata or {}
    links: dict[str, str] = {"feedback_id": item.feedback_id}
    if item.evidence.trace_id:
        links["trace_id"] = item.evidence.trace_id
    for key in ["incident_id", "report_id", "session_id"]:
        value = str(metadata.get(key) or "").strip()
        if value:
            links[key] = value
    return links


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


def _store_has_report(store: Any, incident_id: str, report_id: str) -> bool:
    if not incident_id:
        return False
    getter = getattr(store, "get_report", None)
    report = getter(report_id) if callable(getter) else store.get_latest_report(incident_id)
    return bool(report and report.incident_id == incident_id)


def _store_has_session(store: Any, incident_id: str, session_id: str) -> bool:
    snapshot = store.get_aiops_session_snapshot(session_id)
    return bool(snapshot and (not incident_id or snapshot.incident_id == incident_id))


def _store_has_trace(store: Any, incident_id: str, trace_id: str) -> bool:
    events = store.list_trace_events(incident_id=incident_id or None, trace_id=trace_id)
    return bool(events)


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def normalize_feedback_owner(owner_id: str) -> str:
    """Return a bounded owner identifier for feedback isolation."""
    normalized = str(owner_id or "anonymous").strip()
    return normalized[:128] or "anonymous"


def diagnosis_feedback_dedupe_fingerprint(
    owner_id: str,
    incident_id: str,
    report_id: str,
) -> str:
    """Identify one owner's mutable vote for one diagnosis report."""
    return _feedback_fingerprint("diagnosis", owner_id, incident_id, report_id)


def direct_feedback_dedupe_fingerprint(
    owner_id: str,
    payload: BadCaseFeedbackCreate,
) -> str:
    """Identify one direct feedback object for idempotent retry/update handling."""
    object_key = (
        payload.idempotency_key
        or str(payload.metadata.get("feedback_object_id") or "")
        or str(payload.metadata.get("message_id") or "")
        or str(payload.metadata.get("report_id") or "")
        or str(payload.metadata.get("run_id") or "")
        or str(payload.metadata.get("session_id") or "")
        or payload.trace_id
    )
    if not object_key:
        object_key = json.dumps(
            payload.model_dump(mode="json", exclude={"idempotency_key"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return _feedback_fingerprint("direct", owner_id, payload.target, object_key)


def sanitize_feedback_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets and PII before any feedback record is persisted."""
    return as_dict(
        redact_sensitive_data(
            payload,
            redact_auth_scheme=True,
        )
    )


def _feedback_fingerprint(*parts: str) -> str:
    canonical = "\x1f".join(str(part or "").strip() for part in parts)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _latest_feedback_items(
    items: list[FeedbackItem],
) -> list[FeedbackItem]:
    """Collapse legacy duplicate JSONL rows while retaining the latest state."""
    latest: dict[str, FeedbackItem] = {}
    for item in items:
        key = getattr(item, "dedupe_fingerprint", "") or item.feedback_id
        current = latest.get(key)
        if current is None or (item.updated_at, item.created_at) > (
            current.updated_at,
            current.created_at,
        ):
            latest[key] = item
    return sorted(latest.values(), key=lambda item: item.updated_at, reverse=True)


def _latest_backlog_items(items: list[EvalBacklogItem]) -> list[EvalBacklogItem]:
    """Collapse duplicate backlog rows by their stable identity."""
    latest: dict[str, EvalBacklogItem] = {}
    for item in items:
        key = item.backlog_id or item.suggested_eval_case_id or item.feedback_id
        current = latest.get(key)
        if current is None or (item.updated_at, item.created_at) > (
            current.updated_at,
            current.created_at,
        ):
            latest[key] = item
    return sorted(latest.values(), key=lambda item: item.updated_at, reverse=True)


def _find_feedback_record_index(
    records: list[dict[str, Any]],
    *,
    record_type: str,
    dedupe_fingerprint: str,
) -> int | None:
    for index, record in enumerate(records):
        if record.get("record_type") != record_type:
            continue
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("dedupe_fingerprint") == dedupe_fingerprint:
            return index
    return None


def _remove_related_records(
    records: list[dict[str, Any]],
    feedback_id: str,
    *,
    include_bad_case: bool,
) -> None:
    removable_types = {"eval_backlog"}
    if include_bad_case:
        removable_types.add("bad_case")
    records[:] = [
        record
        for record in records
        if not (
            record.get("record_type") in removable_types
            and isinstance(record.get("payload"), dict)
            and record["payload"].get("feedback_id") == feedback_id
        )
    ]


def _backlog_query(
    backlog_payload: dict[str, Any],
    primary: DiagnosisFeedback | BadCaseFeedback,
) -> str:
    snapshot = backlog_payload.get("evidence_snapshot")
    if isinstance(snapshot, dict) and str(snapshot.get("query") or "").strip():
        return str(snapshot["query"]).strip()
    if isinstance(primary, BadCaseFeedback):
        return primary.evidence.query.strip() or primary.reason.strip()
    return primary.incident_id


class _FeedbackStorageLock:
    """Combine thread and process locks for JSONL read-modify-write operations."""

    def __init__(self, thread_lock: threading.RLock, file_lock: FileLock):
        self.thread_lock = thread_lock
        self.file_lock = file_lock

    def __enter__(self) -> _FeedbackStorageLock:
        self.thread_lock.acquire()
        try:
            self.file_lock.acquire()
        except Exception:
            self.thread_lock.release()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.file_lock.release()
        self.thread_lock.release()


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


def _read_raw_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _parse_record_line(line: str) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


def stable_eval_case_id(prefix: str, source: str) -> str:
    """Build a readable deterministic eval-case id from feedback content."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", source.lower()).strip("_")
    if not slug:
        slug = "case"
    return f"{prefix}_{slug[:48]}"


feedback_service = FeedbackService()
