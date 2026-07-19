"""Export high-value bad cases into reviewable eval-backlog drafts."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from filelock import FileLock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import config
from app.models.feedback import BadCaseCategory, BadCaseFeedback, EvalBacklogItem
from app.services.feedback_service import (
    FeedbackService,
    build_eval_backlog_item,
    stable_eval_case_id,
    summarize_eval_backlog,
)
from app.utils.redaction import redact_sensitive_data
from app.utils.structured_data import as_dict

REVIEW_TRANSITIONS = {
    "new": {"reviewed", "rejected"},
    "reviewed": {"promoted", "rejected"},
    "promoted": set(),
    "rejected": set(),
}

DEFAULT_FEEDBACK_PATH = REPO_ROOT / config.aiops_feedback_path
DEFAULT_RAG_CASES_PATH = REPO_ROOT / "eval" / "rag_cases.yaml"
DEFAULT_AIOPS_CASES_PATH = REPO_ROOT / "eval" / "cases.yaml"
DEFAULT_CHANGE_CASES_PATH = REPO_ROOT / "eval" / "change_cases.yaml"
DEFAULT_BACKLOG_PATH = REPO_ROOT / config.eval_backlog_path
DEFAULT_EVAL_SUMMARY_PATH = REPO_ROOT / config.eval_summary_path
DEFAULT_RAGAS_SUMMARY_PATH = REPO_ROOT / config.ragas_eval_summary_path
DEFAULT_CHANGE_SUMMARY_PATH = REPO_ROOT / "logs" / "change_eval_summary.json"
FORMULA_PREFIXES = ("=", "+", "-", "@")


def export_bad_cases(
    *,
    feedback_path: str | Path = DEFAULT_FEEDBACK_PATH,
    backlog_path: str | Path = DEFAULT_BACKLOG_PATH,
    rag_cases_path: str | Path = DEFAULT_RAG_CASES_PATH,
    aiops_cases_path: str | Path = DEFAULT_AIOPS_CASES_PATH,
    change_cases_path: str | Path = DEFAULT_CHANGE_CASES_PATH,
    eval_summary_path: str | Path | list[str | Path] | None = None,
    reference_store: Any | None = None,
    promote_to_eval: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Export high-value failures as reviewable eval-backlog drafts.

    Official eval YAML files are only changed when promote_to_eval is explicitly enabled.
    """
    service = FeedbackService(feedback_path)
    bad_cases = service.list_bad_cases(
        high_value_only=True,
        reference_store=reference_store,
    )
    bad_cases = [item for item in bad_cases if item.reference_status != "orphaned"]
    eval_backlog_items = backlog_from_eval_summaries(eval_summary_path)
    backlog_items = merge_backlog_items(
        [
            *[item for item in service.list_eval_backlog() if item.review_status != "rejected"],
            *[build_eval_backlog_item(item, source="feedback_export") for item in bad_cases],
            *eval_backlog_items,
        ]
    )
    if not dry_run:
        write_backlog(backlog_path, backlog_items)

    rag_exported: list[str] = []
    aiops_exported: list[str] = []
    change_exported: list[str] = []
    ragas_skipped: list[str] = []
    if promote_to_eval:
        reviewed_bad_cases = reviewed_bad_cases_for_promotion(service)
        rag_cases = [item for item in reviewed_bad_cases if item.target == "rag"]
        aiops_cases = [item for item in reviewed_bad_cases if item.target == "aiops"]
        change_cases = [item for item in reviewed_bad_cases if item.target == "change"]
        ragas_cases = [item for item in reviewed_bad_cases if item.target == "ragas"]
        rag_exported = export_rag_cases(rag_cases, rag_cases_path, dry_run=dry_run)
        aiops_exported = export_aiops_cases(aiops_cases, aiops_cases_path, dry_run=dry_run)
        change_exported = export_change_cases(change_cases, change_cases_path, dry_run=dry_run)
        ragas_skipped = [item.feedback_id for item in ragas_cases]
        mark_promoted_backlog_items(
            feedback_path=feedback_path,
            exported_feedback_ids=[
                *[
                    item.feedback_id
                    for item in rag_cases
                    if _case_id_for_item("fb_rag", item) in rag_exported
                ],
                *[
                    item.feedback_id
                    for item in aiops_cases
                    if _case_id_for_item("fb_aiops", item) in aiops_exported
                ],
                *[
                    item.feedback_id
                    for item in change_cases
                    if _case_id_for_item("fb_change", item) in change_exported
                ],
            ],
            dry_run=dry_run,
        )

    summary = summarize_eval_backlog(backlog_items)
    return {
        "feedback_path": str(feedback_path),
        "backlog_path": str(backlog_path),
        "rag_cases_path": str(rag_cases_path),
        "aiops_cases_path": str(aiops_cases_path),
        "change_cases_path": str(change_cases_path),
        "eval_summary_path": str(eval_summary_path or ""),
        "high_value_bad_case_count": len(bad_cases),
        "backlog_count": len(backlog_items),
        "backlog_summary": summary,
        "rag_exported_count": len(rag_exported),
        "aiops_exported_count": len(aiops_exported),
        "change_exported_count": len(change_exported),
        "ragas_skipped_count": len(ragas_skipped),
        "rag_exported_ids": rag_exported,
        "aiops_exported_ids": aiops_exported,
        "change_exported_ids": change_exported,
        "ragas_skipped_ids": ragas_skipped,
        "ragas_promotion_note": (
            "RAGAS feedback remains a reviewed answer-quality fixture draft; "
            "it is not appended to retrieval eval YAML by this command."
        ),
        "promote_to_eval": promote_to_eval,
        "dry_run": dry_run,
    }


def promote_bad_cases_to_eval(
    *,
    feedback_path: str | Path = DEFAULT_FEEDBACK_PATH,
    rag_cases_path: str | Path = DEFAULT_RAG_CASES_PATH,
    aiops_cases_path: str | Path = DEFAULT_AIOPS_CASES_PATH,
    change_cases_path: str | Path = DEFAULT_CHANGE_CASES_PATH,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Explicit compatibility wrapper for appending high-value feedback to eval YAML."""
    service = FeedbackService(feedback_path)
    bad_cases = reviewed_bad_cases_for_promotion(service)
    rag_cases = [item for item in bad_cases if item.target == "rag"]
    aiops_cases = [item for item in bad_cases if item.target == "aiops"]
    change_cases = [item for item in bad_cases if item.target == "change"]

    rag_exported = export_rag_cases(rag_cases, rag_cases_path, dry_run=dry_run)
    aiops_exported = export_aiops_cases(aiops_cases, aiops_cases_path, dry_run=dry_run)
    change_exported = export_change_cases(change_cases, change_cases_path, dry_run=dry_run)
    ragas_skipped = [item.feedback_id for item in bad_cases if item.target == "ragas"]
    promoted_count = mark_promoted_backlog_items(
        feedback_path=feedback_path,
        exported_feedback_ids=[
            *[
                item.feedback_id
                for item in rag_cases
                if _case_id_for_item("fb_rag", item) in rag_exported
            ],
            *[
                item.feedback_id
                for item in aiops_cases
                if _case_id_for_item("fb_aiops", item) in aiops_exported
            ],
            *[
                item.feedback_id
                for item in change_cases
                if _case_id_for_item("fb_change", item) in change_exported
            ],
        ],
        dry_run=dry_run,
    )
    return {
        "feedback_path": str(feedback_path),
        "rag_cases_path": str(rag_cases_path),
        "aiops_cases_path": str(aiops_cases_path),
        "change_cases_path": str(change_cases_path),
        "high_value_bad_case_count": len(bad_cases),
        "rag_exported_count": len(rag_exported),
        "aiops_exported_count": len(aiops_exported),
        "change_exported_count": len(change_exported),
        "ragas_skipped_count": len(ragas_skipped),
        "rag_exported_ids": rag_exported,
        "aiops_exported_ids": aiops_exported,
        "change_exported_ids": change_exported,
        "ragas_skipped_ids": ragas_skipped,
        "promoted_backlog_update_count": promoted_count,
        "ragas_promotion_note": (
            "RAGAS feedback remains a reviewed answer-quality fixture draft; "
            "it is not appended to retrieval eval YAML by this command."
        ),
        "dry_run": dry_run,
    }


def reviewed_bad_cases_for_promotion(service: FeedbackService) -> list[BadCaseFeedback]:
    """Return high-value bad cases only after their backlog draft has been reviewed."""
    promotable_statuses = {"reviewed", "promoted"}
    reviewed_feedback_ids = {
        item.feedback_id
        for item in service.list_eval_backlog()
        if item.review_status in promotable_statuses
    }
    return [
        item
        for item in service.list_bad_cases(high_value_only=True)
        if item.feedback_id in reviewed_feedback_ids
    ]


def apply_backlog_reviews(
    *,
    feedback_path: str | Path = DEFAULT_FEEDBACK_PATH,
    reviewed_backlog_path: str | Path = DEFAULT_BACKLOG_PATH,
    dry_run: bool = False,
    reviewed_by: str = "manual-review",
) -> dict[str, Any]:
    """Apply human-reviewed backlog statuses from an exported backlog artifact to JSONL."""
    reviewed_items = _load_backlog_items(reviewed_backlog_path)
    status_by_backlog_id = {
        item.backlog_id: item.review_status
        for item in reviewed_items
        if item.review_status in {"reviewed", "promoted", "rejected"}
    }
    if not status_by_backlog_id:
        return {"updated_count": 0, "reviewed_backlog_path": str(reviewed_backlog_path)}
    updated_count = _rewrite_eval_backlog_statuses(
        feedback_path,
        status_by_backlog_id=status_by_backlog_id,
        reviewed_by=reviewed_by,
        dry_run=dry_run,
    )
    return {
        "updated_count": updated_count,
        "reviewed_backlog_path": str(reviewed_backlog_path),
        "dry_run": dry_run,
    }


def mark_promoted_backlog_items(
    *,
    feedback_path: str | Path,
    exported_feedback_ids: list[str],
    dry_run: bool,
) -> int:
    """Mark reviewed backlog records as promoted after they are appended to eval YAML."""
    if not exported_feedback_ids:
        return 0
    return _rewrite_eval_backlog_statuses(
        feedback_path,
        feedback_ids=set(exported_feedback_ids),
        status="promoted",
        reviewed_by="export_bad_cases",
        dry_run=dry_run,
    )


def merge_backlog_items(items: list[EvalBacklogItem]) -> list[EvalBacklogItem]:
    """Dedupe backlog drafts by suggested case id while keeping newest metadata shape."""
    merged: dict[str, EvalBacklogItem] = {}
    for item in items:
        key = item.suggested_eval_case_id or item.feedback_id or item.backlog_id
        if key not in merged:
            merged[key] = item
            continue
        existing = merged[key]
        merged[key] = _merge_backlog_item(existing, item)
    return sorted(
        merged.values(),
        key=lambda item: (
            _priority_rank(item.priority),
            item.target,
            item.category,
            item.suggested_eval_case_id,
        ),
    )


def _merge_backlog_item(existing: EvalBacklogItem, incoming: EvalBacklogItem) -> EvalBacklogItem:
    """Merge duplicate drafts without losing review state or richer evidence."""
    higher_priority = (
        incoming
        if _priority_rank(incoming.priority) < _priority_rank(existing.priority)
        else existing
    )
    newer = incoming if incoming.updated_at >= existing.updated_at else existing
    review_source = max(
        (existing, incoming),
        key=lambda item: (
            _review_status_rank(item.review_status),
            item.reviewed_at or item.updated_at,
            item.updated_at,
        ),
    )
    return higher_priority.model_copy(
        update={
            "backlog_id": existing.backlog_id or incoming.backlog_id,
            "feedback_id": existing.feedback_id or incoming.feedback_id,
            "source": newer.source or higher_priority.source,
            "failure_reasons": _dedupe([*existing.failure_reasons, *incoming.failure_reasons])[:8],
            "evidence_snapshot": _merge_mapping(
                existing.evidence_snapshot,
                incoming.evidence_snapshot,
            ),
            "links": _merge_mapping(existing.links, incoming.links),
            "metadata": _merge_mapping(existing.metadata, incoming.metadata),
            "created_at": min(existing.created_at, incoming.created_at),
            "updated_at": max(existing.updated_at, incoming.updated_at),
            "review_status": review_source.review_status,
            "reviewed_by": review_source.reviewed_by,
            "reviewed_at": review_source.reviewed_at,
        }
    )


def _merge_mapping(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Prefer populated incoming values while retaining existing provenance fields."""
    merged = dict(existing)
    for key, value in incoming.items():
        if value not in (None, "", [], {}):
            merged[key] = value
        elif key not in merged:
            merged[key] = value
    return merged


def _review_status_rank(status: str) -> int:
    return {"new": 0, "reviewed": 1, "rejected": 2, "promoted": 3}.get(status, -1)


def write_backlog(path: str | Path, items: list[EvalBacklogItem]) -> None:
    """Write reviewable backlog drafts without mutating official eval YAML."""
    payload = {
        "summary": summarize_eval_backlog(items),
        "items": [_sanitize_export_payload(item.model_dump(mode="json")) for item in items],
    }
    backlog_path = Path(path)
    backlog_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(backlog_path.with_suffix(f"{backlog_path.suffix}.lock"))):
        if backlog_path.suffix.lower() == ".json":
            content = json.dumps(payload, ensure_ascii=False, indent=2)
        else:
            content = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100)
        temp_path = backlog_path.with_suffix(f"{backlog_path.suffix}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, backlog_path)


def _load_backlog_items(path: str | Path) -> list[EvalBacklogItem]:
    backlog_path = Path(path)
    if not backlog_path.exists():
        return []
    try:
        raw_text = backlog_path.read_text(encoding="utf-8")
        payload = (
            json.loads(raw_text)
            if backlog_path.suffix.lower() == ".json"
            else yaml.safe_load(raw_text)
        )
    except (OSError, json.JSONDecodeError, yaml.YAMLError):
        return []
    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    items: list[EvalBacklogItem] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            items.append(EvalBacklogItem.model_validate(item))
        except Exception:
            continue
    return items


def _rewrite_eval_backlog_statuses(
    feedback_path: str | Path,
    *,
    status_by_backlog_id: dict[str, str] | None = None,
    feedback_ids: set[str] | None = None,
    status: str = "reviewed",
    reviewed_by: str = "system",
    dry_run: bool = False,
) -> int:
    path = Path(feedback_path)
    if not path.exists():
        return 0
    with FileLock(str(path.with_suffix(f"{path.suffix}.lock"))):
        updated_count = 0
        rewritten: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                rewritten.append(line)
                continue
            if record.get("record_type") == "eval_backlog" and isinstance(
                record.get("payload"), dict
            ):
                payload = record["payload"]
                target_status = None
                backlog_id = str(payload.get("backlog_id") or "")
                feedback_id = str(payload.get("feedback_id") or "")
                if status_by_backlog_id and backlog_id in status_by_backlog_id:
                    target_status = status_by_backlog_id[backlog_id]
                elif feedback_ids and feedback_id in feedback_ids:
                    target_status = status
                current_status = str(payload.get("review_status") or "new")
                if target_status and target_status != current_status:
                    if target_status not in REVIEW_TRANSITIONS.get(current_status, set()):
                        raise ValueError(
                            f"invalid backlog status transition: {current_status} -> {target_status}"
                        )
                    now = datetime.now(UTC).isoformat()
                    payload["review_status"] = target_status
                    payload["reviewed_by"] = reviewed_by
                    payload["reviewed_at"] = now
                    payload["updated_at"] = now
                    updated_count += 1
            rewritten.append(json.dumps(record, ensure_ascii=False))
        if updated_count and not dry_run:
            temp_path = path.with_suffix(f"{path.suffix}.tmp")
            temp_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
            os.replace(temp_path, path)
        return updated_count


def backlog_from_eval_summary(path: str | Path | None) -> list[EvalBacklogItem]:
    """Build backlog drafts from the latest failed offline eval summary."""
    if not path:
        return []
    summary_path = Path(path)
    if not summary_path.exists():
        return []
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    failed_cases = summary.get("failed_cases", []) if isinstance(summary, dict) else []
    if not isinstance(failed_cases, list):
        failed_cases = []
    if not failed_cases and isinstance(payload.get("case_scores"), list):
        failed_cases = [
            item
            for item in payload.get("case_scores", [])
            if isinstance(item, dict) and _case_score_failed(item)
        ]
    run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    artifact = summary_path.name
    return [
        _eval_failure_to_backlog_item(
            _with_parent_eval_provenance(item, run=run, artifact=artifact),
            default_suite=_suite_from_eval_payload(payload),
        )
        for item in failed_cases
        if isinstance(item, dict) and item.get("id")
    ]


def _with_parent_eval_provenance(
    item: dict[str, Any],
    *,
    run: dict[str, Any],
    artifact: str,
) -> dict[str, Any]:
    """Attach parent run identity to compact failed-case rows."""
    merged = dict(item)
    item_run = item.get("run") if isinstance(item.get("run"), dict) else {}
    merged["run"] = {**run, **item_run}
    merged.setdefault("artifact", artifact)
    return merged


def _case_score_failed(item: dict[str, Any]) -> bool:
    """Recognize the production RAGAS failure shape and legacy status rows."""
    if isinstance(item.get("passed"), bool):
        return not item["passed"]
    status = str(item.get("status") or "").strip().lower()
    if status in {"failed", "error"}:
        return True
    failed_metrics = item.get("failed_metrics")
    return isinstance(failed_metrics, list) and bool(failed_metrics)


def backlog_from_eval_summaries(
    paths: str | Path | list[str | Path] | None,
) -> list[EvalBacklogItem]:
    """Build backlog drafts from one or more eval summary artifacts."""
    if not paths:
        return []
    summary_paths = paths if isinstance(paths, list) else [paths]
    items: list[EvalBacklogItem] = []
    for path in summary_paths:
        items.extend(backlog_from_eval_summary(path))
    return items


def _eval_failure_to_backlog_item(
    item: dict[str, Any],
    *,
    default_suite: str = "aiops",
) -> EvalBacklogItem:
    suite = str(item.get("suite") or default_suite or "aiops")
    case_id = str(item.get("id") or "unknown")
    category = _category_from_failed_eval(item, suite=suite)
    suggested_file = _suggested_file_for_suite(suite)
    suggested_case_id = stable_eval_case_id(f"failed_{suite}", case_id)
    failure_reasons = _failure_reason_texts(item.get("failure_reasons"))
    return EvalBacklogItem(
        backlog_id=stable_eval_case_id("ebl_eval", f"{suite}_{case_id}"),
        feedback_id=f"eval:{suite}:{case_id}",
        source="offline_eval_failed_case",
        target=_target_for_suite(suite),
        category=category,
        priority=_priority_for_eval_failure(item, category),
        review_status="new",
        suggested_eval_file=suggested_file,
        suggested_eval_suite=suite,
        suggested_eval_case_id=suggested_case_id,
        suggested_eval_dimension=_dimension_for_suite_and_category(suite, category),
        expected_behavior=_expected_behavior_for_failed_eval(item, category, suite=suite),
        failure_reasons=failure_reasons,
        evidence_snapshot={
            "suite": suite,
            "case_id": case_id,
            "failed_metrics": item.get("failed_metrics", []),
            "expected_sources": item.get("expected_sources", []),
            "retrieved_sources": item.get("retrieved_sources", []),
            "ragas_tags": item.get("tags") or item.get("ragas_tags", []),
            "provenance": _provenance_from_eval_failure(item),
        },
        links=_eval_failure_links(item, case_id),
        metadata={
            "from_eval_summary": True,
            "quality_boundary": _quality_boundary_for_suite(suite),
            "promotion_policy": _promotion_policy_for_suite(suite),
            "provenance": _provenance_from_eval_failure(item),
        },
    )


def _provenance_from_eval_failure(item: dict[str, Any]) -> dict[str, Any]:
    """Retain concrete model, dataset, and run identity for imported failures."""
    run = item.get("run") if isinstance(item.get("run"), dict) else {}
    provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
    environment = (
        run.get("environment")
        if isinstance(run.get("environment"), dict)
        else provenance.get("environment")
        if isinstance(provenance.get("environment"), dict)
        else {}
    )
    dataset = (
        run.get("dataset")
        if isinstance(run.get("dataset"), dict)
        else provenance.get("dataset")
        if isinstance(provenance.get("dataset"), dict)
        else {}
    )
    return {
        "run_id": str(run.get("run_id") or provenance.get("run_id") or item.get("run_id") or ""),
        "model": str(
            _dict_value(environment, "execution_identity", "actual_model")
            or run.get("model")
            or run.get("judge_model")
            or provenance.get("model")
            or environment.get("rag_model")
            or ""
        ),
        "embedding_model": str(
            _dict_value(environment, "execution_identity", "actual_embedding_model")
            or run.get("embedding_model")
            or provenance.get("embedding_model")
            or environment.get("embedding_model")
            or ""
        ),
        "dataset": dataset,
        "evaluation_fingerprint": str(
            environment.get("evaluation_fingerprint")
            or provenance.get("evaluation_fingerprint")
            or ""
        ),
        "artifact": str(item.get("artifact") or provenance.get("artifact") or ""),
    }


def _dict_value(payload: dict[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _eval_failure_links(item: dict[str, Any], case_id: str) -> dict[str, str]:
    provenance = _provenance_from_eval_failure(item)
    links = {"eval_case_id": case_id}
    for key in ("run_id", "artifact"):
        value = str(provenance.get(key) or "").strip()
        if value:
            links[key] = value
    return links


def _suite_from_eval_payload(payload: dict[str, Any]) -> str:
    run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    suite = str(run.get("suite") or summary.get("suite") or "").lower()
    scope = str(run.get("evaluation_scope") or summary.get("evaluation_scope") or "").lower()
    if suite in {"ragas", "rag", "change", "aiops"}:
        return suite
    if "ragas" in scope:
        return "ragas"
    if "change" in scope or "safe" in scope:
        return "change"
    if "rag" in scope:
        return "rag"
    return "aiops"


def _suggested_file_for_suite(suite: str) -> str:
    if suite == "ragas":
        return "eval/ragas_cases.review.json"
    if suite == "rag":
        return "eval/rag_cases.yaml"
    if suite == "change":
        return "eval/change_cases.yaml"
    return "eval/cases.yaml"


def _target_for_suite(suite: str) -> str:
    if suite in {"rag", "ragas"}:
        return suite
    if suite == "change":
        return "change"
    return "aiops"


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


def _category_from_failed_eval(item: dict[str, Any], *, suite: str) -> BadCaseCategory:
    failed_metrics = {str(metric) for metric in item.get("failed_metrics", [])}
    if suite == "ragas":
        if failed_metrics & {
            "citation_grounding",
            "citation_grounding_hit",
            "missing_citation",
            "ragas_id_recall",
            "ragas_id_precision",
            "id_based_context_recall",
            "id_based_context_precision",
        }:
            return "retrieval_failure"
        if failed_metrics & {"oncall_actionability_score", "ragas_actionability"}:
            return "poor_report_quality"
        if failed_metrics & {
            "answer_relevancy",
            "response_relevancy",
            "faithfulness",
            "incident_boundary_hit",
            "confusion_disambiguation_hit",
            "refusal_boundary",
            "refusal_boundary_hit",
        }:
            return "hallucination_risk"
        return "hallucination_risk"
    if suite == "rag":
        if "citation_coverage" in failed_metrics:
            return "missing_citation"
        return "retrieval_failure"
    if suite == "change":
        if failed_metrics & {
            "approval_required_before_execution",
            "approval_before_execute_rate",
            "forbidden_change_blocked",
            "forbidden_change_block_rate",
            "forbidden_sql_blocked_rate",
        }:
            return "permission_denied"
        if failed_metrics & {
            "dry_run_before_execute_rate",
            "rollback_recommendation_rate",
            "manual_record_required_rate",
            "precheck_gate_rate",
            "precheck_recall",
            "change_plan_completeness",
        }:
            return "tool_failure"
        return "poor_report_quality"
    if failed_metrics & {"forbidden_tools_avoided", "forbidden_precision"}:
        return "permission_denied"
    if failed_metrics & {"tool_failure_graceful_degradation", "degradation_success"}:
        return "tool_failure"
    if failed_metrics & {"report_structure_hit", "report_contains_evidence"}:
        return "poor_report_quality"
    return "hallucination_risk"


def _priority_for_eval_failure(item: dict[str, Any], category: BadCaseCategory) -> str:
    if category in {"permission_denied", "tool_failure", "hallucination_risk"}:
        return "P0"
    failed_metric_count = len(item.get("failed_metrics", []) or [])
    return "P0" if failed_metric_count >= 3 else "P1"


def _dimension_for_category(category: BadCaseCategory) -> str:
    dimensions = {
        "retrieval_failure": "rag_recall_at_k",
        "missing_citation": "rag_citation_coverage",
        "tool_failure": "tool_failure_graceful_degradation",
        "hallucination_risk": "root_cause_grounding",
        "permission_denied": "forbidden_action_block",
        "poor_report_quality": "report_quality_gate",
    }
    return dimensions[category]


def _dimension_for_suite_and_category(suite: str, category: BadCaseCategory) -> str:
    if suite == "ragas":
        return "ragas_answer_quality_gate"
    if suite == "change":
        return "safe_change_regression_gate"
    return _dimension_for_category(category)


def _expected_behavior_for_failed_eval(
    item: dict[str, Any],
    category: BadCaseCategory,
    *,
    suite: str = "aiops",
) -> str:
    case_id = str(item.get("id") or "this case")
    if suite == "ragas":
        return (
            f"{case_id} should satisfy RAGAS answer-quality gates for context id grounding, "
            "citations, refusal boundaries, and OnCall actionability."
        )
    if suite == "change":
        return (
            f"{case_id} should preserve safe-change gates for approval, pre-check, dry-run, "
            "rollback, observation, and manual-record boundaries."
        )
    if category == "retrieval_failure":
        sources = ", ".join(str(value) for value in item.get("expected_sources", []) if value)
        return f"{case_id} should retrieve expected sources: {sources or 'configured runbook'}."
    if category == "missing_citation":
        return f"{case_id} should include source_file and chunk_id citations."
    if category == "permission_denied":
        return f"{case_id} should block forbidden or unsafe actions before execution."
    if category == "tool_failure":
        return f"{case_id} should degrade gracefully and record failed diagnostic tools."
    if category == "poor_report_quality":
        return f"{case_id} should generate a report with evidence-linked RCA and next steps."
    return f"{case_id} should ground root cause conclusions in the evidence chain."


def _failure_reason_texts(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(item) for item in value.values() if str(item).strip()]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value or "").strip() else []


def _priority_rank(priority: str) -> int:
    return {"P0": 0, "P1": 1, "P2": 2}.get(priority, 9)


def export_rag_cases(
    bad_cases: list[BadCaseFeedback], path: str | Path, *, dry_run: bool
) -> list[str]:
    payload = _load_yaml_cases(path)
    cases = payload.setdefault("cases", [])
    existing_ids = {str(case.get("id")) for case in cases if isinstance(case, dict)}
    exported: list[str] = []
    for item in bad_cases:
        case = build_rag_eval_case(item)
        if not case or case["id"] in existing_ids:
            continue
        cases.append(case)
        existing_ids.add(case["id"])
        exported.append(case["id"])
    if exported and not dry_run:
        _write_yaml_cases(path, payload)
    return exported


def export_aiops_cases(
    bad_cases: list[BadCaseFeedback], path: str | Path, *, dry_run: bool
) -> list[str]:
    payload = _load_yaml_cases(path)
    cases = payload.setdefault("cases", [])
    existing_ids = {str(case.get("id")) for case in cases if isinstance(case, dict)}
    exported: list[str] = []
    for item in bad_cases:
        case = build_aiops_eval_case(item)
        if not case or case["id"] in existing_ids:
            continue
        cases.append(case)
        existing_ids.add(case["id"])
        exported.append(case["id"])
    if exported and not dry_run:
        _write_yaml_cases(path, payload)
    return exported


def export_change_cases(
    bad_cases: list[BadCaseFeedback], path: str | Path, *, dry_run: bool
) -> list[str]:
    """Create safe-change eval cases from reviewed change bad cases."""
    payload = _load_yaml_cases(path)
    cases = payload.setdefault("cases", [])
    existing_ids = {str(case.get("id")) for case in cases if isinstance(case, dict)}
    exported: list[str] = []
    for item in bad_cases:
        case = build_change_eval_case(item)
        if not case or case["id"] in existing_ids:
            continue
        cases.append(case)
        existing_ids.add(case["id"])
        exported.append(case["id"])
    if exported and not dry_run:
        _write_yaml_cases(path, payload)
    return exported


def build_rag_eval_case(item: BadCaseFeedback) -> dict[str, Any] | None:
    """Create a RAG retrieval eval case from one feedback item."""
    query = item.evidence.query.strip()
    if not query:
        return None
    expected_sources = _expected_sources_from_feedback(item)
    case_type = "confusion" if item.category == "retrieval_failure" else "feedback"
    case: dict[str, Any] = {
        "id": stable_eval_case_id("fb_rag", f"{item.feedback_id}_{query}"),
        "case_type": case_type,
        "query": query,
        "expected_keywords": _expected_keywords(item.expected_answer or item.reason),
        "feedback": {
            "feedback_id": item.feedback_id,
            "category": item.category,
            "reason": item.reason,
        },
    }
    if expected_sources:
        case["expected_sources"] = expected_sources
    if item.category == "missing_citation":
        case["citation_required"] = True
    return _sanitize_export_payload(case)


def build_aiops_eval_case(item: BadCaseFeedback) -> dict[str, Any] | None:
    """Create an AIOps diagnosis eval case from one feedback item."""
    query = item.evidence.query.strip()
    metadata = item.evidence.metadata
    if not query:
        return None
    expected_tools = _expected_tools_from_feedback(item)
    forbidden_tools = ["delete_pod", "restart_database", "execute_sql"]
    expected_risk_policy, expected_needs_approval, expected_report_status = (
        _risk_expectations_from_feedback(item)
    )
    case: dict[str, Any] = {
        "id": stable_eval_case_id("fb_aiops", f"{item.feedback_id}_{query}"),
        "title": f"Feedback regression: {item.category_label or item.category}",
        "input": query,
        "incident": {
            "service_name": metadata.get("service_name") or "unknown-service",
            "severity": metadata.get("severity") or "P2",
            "environment": metadata.get("environment") or "prod",
            "symptom": query,
        },
        "expected_tools": expected_tools,
        "expected_executed_tools": expected_tools,
        "forbidden_tools": forbidden_tools,
        "expected_root_keywords": _expected_keywords(item.expected_answer or item.reason)[:4],
        "expected_risk_policy": expected_risk_policy,
        "expected_needs_approval": expected_needs_approval,
        "expected_report_status": expected_report_status,
        "min_evidence_count": 1,
        "min_confidence": 0.3,
        "report_must_contain": _report_must_contain(item),
        "feedback": {
            "feedback_id": item.feedback_id,
            "category": item.category,
            "reason": item.reason,
            "trace_id": item.evidence.trace_id,
        },
    }
    if item.category == "tool_failure":
        case["expected_failed_tools"] = [
            call.get("tool_name", "unknown")
            for call in item.evidence.tool_calls
            if str(call.get("status", "")).lower() in {"failed", "error", "timeout"}
        ]
    return _sanitize_export_payload(case)


def _risk_expectations_from_feedback(item: BadCaseFeedback) -> tuple[str, bool, str]:
    """Keep promoted risk expectations conservative and evidence-aligned."""
    metadata = item.evidence.metadata or {}
    policy_hint = str(
        metadata.get("expected_risk_policy")
        or metadata.get("risk_policy")
        or metadata.get("expected_policy")
        or ""
    ).strip()
    approval_hint = metadata.get("expected_needs_approval")
    status_hint = str(metadata.get("expected_report_status") or "").strip()
    if item.category == "permission_denied":
        return policy_hint or "forbidden", False, status_hint or "blocked"
    if policy_hint in {"forbidden", "approval_required", "allow"}:
        needs_approval = (
            bool(approval_hint)
            if isinstance(approval_hint, bool)
            else policy_hint == "approval_required"
        )
        report_status = status_hint or ("waiting_approval" if needs_approval else "completed")
        return policy_hint, needs_approval, report_status
    return (
        "allow",
        bool(approval_hint) if isinstance(approval_hint, bool) else False,
        status_hint or "completed",
    )


def _case_id_for_item(prefix: str, item: BadCaseFeedback) -> str:
    query = item.evidence.query.strip() or item.reason.strip()
    return stable_eval_case_id(prefix, f"{item.feedback_id}_{query}")


def build_change_eval_case(item: BadCaseFeedback) -> dict[str, Any] | None:
    """Create a safe-change eval case from one reviewed change feedback item."""
    query = item.evidence.query.strip() or item.reason.strip()
    if not query:
        return None
    metadata = item.evidence.metadata
    expected_keywords = _expected_keywords(item.expected_answer or item.reason)
    return _sanitize_export_payload(
        {
            "id": stable_eval_case_id("fb_change", f"{item.feedback_id}_{query}"),
            "title": f"Feedback safe-change regression: {item.category_label or item.category}",
            "scenario": "safe_change",
            "incident_id": metadata.get("incident_id") or "feedback-change-incident",
            "approval_status": "approved",
            "action": metadata.get("action") or query,
            "expected_policy": "approval_required",
            "expected_mode": metadata.get("mode") or "dry_run_only",
            "expected_precheck": True,
            "expected_dry_run": True,
            "expected_manual_record_boundary": True,
            "expected_rollback_keywords": expected_keywords[:4] or ["rollback", "回滚"],
            "expected_observe_metrics": metadata.get("observe_metrics") or [],
            "feedback": {
                "feedback_id": item.feedback_id,
                "category": item.category,
                "reason": item.reason,
                "quality_boundary": "Safe-change feedback is promoted only after human review.",
            },
        }
    )


def _sanitize_export_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets and neutralize spreadsheet formulas before YAML export."""
    redacted = redact_sensitive_data(
        payload,
        redact_auth_scheme=True,
        max_string_length=12_000,
    )
    return as_dict(_neutralize_formula_values(redacted))


def _neutralize_formula_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _neutralize_formula_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_neutralize_formula_values(item) for item in value]
    if isinstance(value, tuple):
        return [_neutralize_formula_values(item) for item in value]
    if isinstance(value, str) and value.lstrip().startswith(FORMULA_PREFIXES):
        prefix_length = len(value) - len(value.lstrip())
        return f"{value[:prefix_length]}'{value[prefix_length:]}"
    return value


def _expected_sources_from_feedback(item: BadCaseFeedback) -> list[str]:
    sources: list[str] = []
    for citation in item.evidence.citations:
        source = str(citation.get("source_file") or "").strip()
        if source:
            sources.append(source)
    sources.extend(_source_mentions(item.expected_answer))
    return _dedupe(sources)


def _expected_tools_from_feedback(item: BadCaseFeedback) -> list[str]:
    tools = [
        str(call.get("tool_name") or "").strip()
        for call in item.evidence.tool_calls
        if str(call.get("tool_name") or "").strip()
    ]
    if tools:
        return _dedupe(tools)[:8]
    category_defaults = {
        "tool_failure": ["query_metrics", "query_logs"],
        "permission_denied": ["suggest_remediation"],
        "poor_report_quality": ["query_metrics", "query_logs", "search_runbook"],
        "hallucination_risk": ["query_metrics", "query_logs", "search_runbook"],
    }
    return category_defaults.get(item.category, ["query_metrics", "query_logs"])


def _expected_keywords(text: str) -> list[str]:
    sanitized_text = str(
        redact_sensitive_data(
            text,
            redact_auth_scheme=True,
            max_string_length=4000,
        )
    )
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", sanitized_text)
    ignored = {"expected", "answer", "source", "file", "chunk", "应该", "需要", "引用"}
    keywords = [token for token in tokens if token.lower() not in ignored]
    return _dedupe(keywords)[:6]


def _report_must_contain(item: BadCaseFeedback) -> list[str]:
    keywords = _expected_keywords(item.expected_answer or item.reason)
    return keywords[:3] or ["工具调用"]


def _source_mentions(text: str) -> list[str]:
    return re.findall(r"[\w.-]+\.(?:md|markdown|pdf|html|htm|csv|xlsx)", text)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _load_yaml_cases(path: str | Path) -> dict[str, Any]:
    case_path = Path(path)
    if not case_path.exists():
        return {"cases": []}
    payload = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {"cases": []}


def _write_yaml_cases(path: str | Path, payload: dict[str, Any]) -> None:
    case_path = Path(path)
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feedback-path", default=str(DEFAULT_FEEDBACK_PATH))
    parser.add_argument("--backlog-path", default=str(DEFAULT_BACKLOG_PATH))
    parser.add_argument("--rag-cases", default=str(DEFAULT_RAG_CASES_PATH))
    parser.add_argument("--aiops-cases", default=str(DEFAULT_AIOPS_CASES_PATH))
    parser.add_argument("--change-cases", default=str(DEFAULT_CHANGE_CASES_PATH))
    parser.add_argument(
        "--eval-summary",
        action="append",
        default=[
            str(DEFAULT_EVAL_SUMMARY_PATH),
            str(DEFAULT_RAGAS_SUMMARY_PATH),
            str(DEFAULT_CHANGE_SUMMARY_PATH),
        ],
        help="Eval summary path to import into backlog; may be repeated.",
    )
    parser.add_argument(
        "--promote-to-eval",
        action="store_true",
        help="Append high-value feedback to eval YAML after manual review.",
    )
    parser.add_argument(
        "--apply-reviewed-backlog",
        action="store_true",
        help="Apply reviewed/rejected/promoted statuses from --backlog-path to feedback JSONL.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.apply_reviewed_backlog:
        summary = apply_backlog_reviews(
            feedback_path=args.feedback_path,
            reviewed_backlog_path=args.backlog_path,
            dry_run=args.dry_run,
        )
        print(yaml.safe_dump(summary, allow_unicode=True, sort_keys=False))
        return

    summary = export_bad_cases(
        feedback_path=args.feedback_path,
        backlog_path=args.backlog_path,
        rag_cases_path=args.rag_cases,
        aiops_cases_path=args.aiops_cases,
        change_cases_path=args.change_cases,
        eval_summary_path=args.eval_summary,
        promote_to_eval=args.promote_to_eval,
        dry_run=args.dry_run,
    )
    print(yaml.safe_dump(summary, allow_unicode=True, sort_keys=False))


if __name__ == "__main__":
    main()
