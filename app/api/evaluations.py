"""Read-only evaluation summary APIs."""

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from app.config import config
from app.core.auth import EVAL_SCOPE, require_scope
from app.models.api_contracts import EvalBacklogResponse, EvalRagasResponse
from app.services.evaluation_read_models import (
    build_adapter_unavailable_payload,
    build_eval_backlog_summary,
    build_eval_summary_payload,
    build_eval_unavailable_payload,
    build_ragas_summary_payload,
    build_ragas_unavailable_payload,
)

router = APIRouter()

EVAL_SUMMARY_PATH = Path(config.eval_summary_path)
EVAL_BACKLOG_PATH = Path(config.eval_backlog_path)
ADAPTER_VERIFICATION_PATH = Path(config.adapter_verification_path)
RAGAS_SUMMARY_PATH = Path(config.ragas_eval_summary_path)


@router.get("/eval/summary", dependencies=[Depends(require_scope(EVAL_SCOPE))])
async def get_eval_summary() -> dict[str, Any]:
    """Return the latest offline evaluation summary for the frontend dashboard."""
    if not EVAL_SUMMARY_PATH.exists():
        return build_eval_unavailable_payload(
            "evaluation summary has not been generated",
            summary_path=EVAL_SUMMARY_PATH,
        )

    try:
        raw_payload = json.loads(EVAL_SUMMARY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return build_eval_unavailable_payload(
            "evaluation summary is unreadable",
            summary_path=EVAL_SUMMARY_PATH,
        )

    if not isinstance(raw_payload, dict):
        return build_eval_unavailable_payload(
            "evaluation summary has an invalid format",
            summary_path=EVAL_SUMMARY_PATH,
        )

    return build_eval_summary_payload(
        raw_payload,
        summary_path=EVAL_SUMMARY_PATH,
        backlog_payload=load_eval_backlog_payload(EVAL_BACKLOG_PATH),
    )


@router.get(
    "/eval/ragas",
    response_model=EvalRagasResponse,
    dependencies=[Depends(require_scope(EVAL_SCOPE))],
)
async def get_ragas_summary() -> dict[str, Any]:
    """Return the latest optional RAGAS quality report for RAG answers."""
    if not RAGAS_SUMMARY_PATH.exists():
        return build_ragas_unavailable_payload(
            "RAGAS quality summary has not been generated",
            summary_path=RAGAS_SUMMARY_PATH,
        )

    try:
        raw_payload = json.loads(RAGAS_SUMMARY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return build_ragas_unavailable_payload(
            "RAGAS quality summary is unreadable",
            summary_path=RAGAS_SUMMARY_PATH,
        )

    if not isinstance(raw_payload, dict):
        return build_ragas_unavailable_payload(
            "RAGAS quality summary has an invalid format",
            summary_path=RAGAS_SUMMARY_PATH,
        )

    return build_ragas_summary_payload(raw_payload, summary_path=RAGAS_SUMMARY_PATH)


@router.get(
    "/eval/ragas-summary",
    response_model=EvalRagasResponse,
    dependencies=[Depends(require_scope(EVAL_SCOPE))],
)
async def get_ragas_summary_alias() -> dict[str, Any]:
    """Return the latest RAGAS quality report using the explicit summary alias."""
    return await get_ragas_summary()


@router.get("/eval/adapter-verification", dependencies=[Depends(require_scope(EVAL_SCOPE))])
async def get_adapter_verification() -> dict[str, Any]:
    """Return the latest full-stack adapter verification payload for the frontend."""
    if not ADAPTER_VERIFICATION_PATH.exists():
        return build_adapter_unavailable_payload(
            "adapter verification has not been generated",
            adapter_path=ADAPTER_VERIFICATION_PATH,
        )

    try:
        raw_payload = json.loads(ADAPTER_VERIFICATION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return build_adapter_unavailable_payload(
            "adapter verification is unreadable",
            adapter_path=ADAPTER_VERIFICATION_PATH,
        )

    if not isinstance(raw_payload, dict):
        return build_adapter_unavailable_payload(
            "adapter verification has an invalid format",
            adapter_path=ADAPTER_VERIFICATION_PATH,
        )

    return {
        **raw_payload,
        "available": True,
        "message": str(raw_payload.get("message") or "adapter verification loaded"),
    }


@router.get(
    "/eval/backlog",
    response_model=EvalBacklogResponse,
    dependencies=[Depends(require_scope(EVAL_SCOPE))],
)
async def get_eval_backlog() -> dict[str, Any]:
    """Return reviewable eval-backlog drafts generated from feedback and failed evals."""
    payload = load_eval_backlog_payload(EVAL_BACKLOG_PATH)
    if payload is None:
        return {
            "available": False,
            "summary": {
                "total": 0,
                "by_target": {},
                "by_category": {},
                "by_priority": {},
                "by_review_status": {},
                "by_eval_file": {},
            },
            "items": [],
        }
    backlog = build_eval_backlog_summary(payload)
    return {
        "available": backlog["available"],
        "summary": backlog["summary"],
        "items": backlog["items"],
    }


def load_eval_backlog_payload(path: Path) -> dict[str, Any] | None:
    """Load optional reviewable eval-backlog drafts for the evaluation dashboard."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
