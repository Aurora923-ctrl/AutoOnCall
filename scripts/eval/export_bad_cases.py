"""Promote high-value feedback bad cases into offline eval YAML files."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import config
from app.models.feedback import BadCaseFeedback
from app.services.feedback_service import FeedbackService, stable_eval_case_id

DEFAULT_FEEDBACK_PATH = REPO_ROOT / config.aiops_feedback_path
DEFAULT_RAG_CASES_PATH = REPO_ROOT / "eval" / "rag_cases.yaml"
DEFAULT_AIOPS_CASES_PATH = REPO_ROOT / "eval" / "cases.yaml"


def export_bad_cases(
    *,
    feedback_path: str | Path = DEFAULT_FEEDBACK_PATH,
    rag_cases_path: str | Path = DEFAULT_RAG_CASES_PATH,
    aiops_cases_path: str | Path = DEFAULT_AIOPS_CASES_PATH,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Append high-value feedback items to eval/rag_cases.yaml and eval/cases.yaml."""
    service = FeedbackService(feedback_path)
    bad_cases = service.list_bad_cases(high_value_only=True)
    rag_cases = [item for item in bad_cases if item.target == "rag"]
    aiops_cases = [item for item in bad_cases if item.target == "aiops"]

    rag_exported = export_rag_cases(rag_cases, rag_cases_path, dry_run=dry_run)
    aiops_exported = export_aiops_cases(aiops_cases, aiops_cases_path, dry_run=dry_run)
    return {
        "feedback_path": str(feedback_path),
        "rag_cases_path": str(rag_cases_path),
        "aiops_cases_path": str(aiops_cases_path),
        "high_value_bad_case_count": len(bad_cases),
        "rag_exported_count": len(rag_exported),
        "aiops_exported_count": len(aiops_exported),
        "rag_exported_ids": rag_exported,
        "aiops_exported_ids": aiops_exported,
        "dry_run": dry_run,
    }


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
    return case


def build_aiops_eval_case(item: BadCaseFeedback) -> dict[str, Any] | None:
    """Create an AIOps diagnosis eval case from one feedback item."""
    query = item.evidence.query.strip()
    metadata = item.evidence.metadata
    if not query:
        return None
    expected_tools = _expected_tools_from_feedback(item)
    forbidden_tools = ["delete_pod", "restart_database", "execute_sql"]
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
        "expected_risk_policy": "forbidden"
        if item.category == "permission_denied"
        else "allow",
        "expected_needs_approval": False,
        "expected_report_status": "completed",
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
    return case


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
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", text)
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
    parser.add_argument("--rag-cases", default=str(DEFAULT_RAG_CASES_PATH))
    parser.add_argument("--aiops-cases", default=str(DEFAULT_AIOPS_CASES_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    summary = export_bad_cases(
        feedback_path=args.feedback_path,
        rag_cases_path=args.rag_cases,
        aiops_cases_path=args.aiops_cases,
        dry_run=args.dry_run,
    )
    print(yaml.safe_dump(summary, allow_unicode=True, sort_keys=False))


if __name__ == "__main__":
    main()

