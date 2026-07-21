"""Feedback persistence, bad-case classification, and eval-case drafting."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar, cast

from filelock import FileLock

from app.config import config
from app.models.feedback import (
    BadCaseFeedback,
    BadCaseFeedbackCreate,
    DiagnosisFeedback,
    DiagnosisFeedbackCreate,
    EvalBacklogItem,
)
from app.models.incident import utc_now
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.feedback_classification import (
    bad_case_payload_from_diagnosis_feedback,
    build_bad_case_feedback,
    build_eval_backlog_item,
    classify_improvement_items,
    dedupe_strings,
    diagnosis_feedback_dedupe_fingerprint,
    direct_feedback_dedupe_fingerprint,
    normalize_diagnosis_feedback_vote,
    normalize_feedback_owner,
    stable_eval_case_id,
    summarize_eval_backlog,
)
from app.utils.redaction import redact_sensitive_data
from app.utils.structured_data import as_dict

FeedbackItem = TypeVar("FeedbackItem", DiagnosisFeedback, BadCaseFeedback)

__all__ = [
    "FeedbackService",
    "build_bad_case_feedback",
    "build_eval_backlog_item",
    "classify_improvement_items",
    "feedback_service",
    "summarize_eval_backlog",
]


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
        if report is not None:
            if report.incident_id != incident_id:
                raise ValueError("report does not belong to incident")
            if payload.report_id != report.report_id:
                raise ValueError("report_id does not match report")
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
        report = None
        snapshot = None
        if not incident_id and any((report_id, session_id)):
            reasons.append("missing_incident_id")
        if store is not None:
            if incident_id and store.get_incident_state(incident_id) is None:
                reasons.append("incident_not_found")
            if report_id:
                report = _store_get_report(store, incident_id, report_id)
                if report is None:
                    reasons.append("report_not_found")
            if session_id:
                snapshot = _store_get_session(store, incident_id, session_id)
                if snapshot is None:
                    reasons.append("session_not_found")
            if item.run_id and session_id and item.run_id != session_id:
                reasons.append("run_session_mismatch")
            if item.run_id and not session_id:
                reasons.append("run_without_session")
            if incident_id and trace_id and not _store_has_trace(store, incident_id, trace_id):
                reasons.append("trace_not_found")
            if (
                report is not None
                and trace_id
                and str(getattr(report, "trace_id", "") or "")
                and str(getattr(report, "trace_id", "")) != trace_id
            ):
                reasons.append("trace_report_mismatch")
            if (
                snapshot is not None
                and trace_id
                and str(getattr(snapshot, "trace_id", "") or "")
                and str(getattr(snapshot, "trace_id", "")) != trace_id
            ):
                reasons.append("trace_session_mismatch")
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
        temp_path = self.storage_path.with_name(
            f".{self.storage_path.name}.{threading.get_ident()}.tmp"
        )
        try:
            temp_path.write_text(
                "\n".join(lines) + ("\n" if lines else ""),
                encoding="utf-8",
            )
            with temp_path.open("r+b") as handle:
                os.fsync(handle.fileno())
            temp_path.replace(self.storage_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()


def _store_has_report(store: Any, incident_id: str, report_id: str) -> bool:
    return _store_get_report(store, incident_id, report_id) is not None


def _store_get_report(store: Any, incident_id: str, report_id: str) -> Any | None:
    if not incident_id:
        return None
    getter = getattr(store, "get_report", None)
    report = getter(report_id) if callable(getter) else store.get_latest_report(incident_id)
    return report if report and report.incident_id == incident_id else None


def _store_has_session(store: Any, incident_id: str, session_id: str) -> bool:
    return _store_get_session(store, incident_id, session_id) is not None


def _store_get_session(store: Any, incident_id: str, session_id: str) -> Any | None:
    snapshot = store.get_aiops_session_snapshot(session_id)
    return snapshot if snapshot and (not incident_id or snapshot.incident_id == incident_id) else None


def _store_has_trace(store: Any, incident_id: str, trace_id: str) -> bool:
    events = store.list_trace_events(incident_id=incident_id or None, trace_id=trace_id)
    return bool(events)


def sanitize_feedback_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets and PII before any feedback record is persisted."""
    return as_dict(
        redact_sensitive_data(
            payload,
            redact_auth_scheme=True,
        )
    )


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


feedback_service = FeedbackService()
