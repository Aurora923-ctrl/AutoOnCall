"""Read-only evaluation summary APIs."""

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from app.config import config
from app.core.auth import EVAL_SCOPE, require_scope
from app.services.evaluation_read_models import (
    build_adapter_unavailable_payload,
    build_eval_summary_payload,
    build_eval_unavailable_payload,
)

router = APIRouter()

EVAL_SUMMARY_PATH = Path(config.eval_summary_path)
ADAPTER_VERIFICATION_PATH = Path(config.adapter_verification_path)


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

    return build_eval_summary_payload(raw_payload, summary_path=EVAL_SUMMARY_PATH)


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
