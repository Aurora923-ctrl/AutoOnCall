"""Feedback APIs for RAG/AIOps bad-case regression loops."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.auth import DIAGNOSE_SCOPE, READ_SCOPE, require_scope
from app.models.api_contracts import (
    BadCaseFeedbackListResponse,
    BadCaseFeedbackResponse,
    EvalBacklogListResponse,
)
from app.models.feedback import BadCaseFeedbackCreate
from app.services.feedback_service import FeedbackService, feedback_service, summarize_eval_backlog

router = APIRouter()


def get_feedback_service() -> FeedbackService:
    """Return the feedback service singleton."""
    return feedback_service


@router.post(
    "/feedback",
    response_model=BadCaseFeedbackResponse,
    dependencies=[Depends(require_scope(DIAGNOSE_SCOPE))],
)
async def submit_bad_case_feedback(payload: BadCaseFeedbackCreate) -> dict:
    """Submit thumb feedback with runtime context for eval-case drafting."""
    feedback = get_feedback_service().submit_bad_case_feedback(payload)
    return {"feedback": feedback}


@router.get(
    "/feedback/bad-cases",
    response_model=BadCaseFeedbackListResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def list_bad_case_feedback(
    target: str | None = Query(default=None),
    high_value_only: bool = Query(default=False),
) -> dict:
    """List captured bad cases that can be promoted into regression cases."""
    return {
        "items": get_feedback_service().list_bad_cases(
            target=target,
            high_value_only=high_value_only,
        )
    }


@router.get(
    "/feedback/eval-backlog",
    response_model=EvalBacklogListResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def list_eval_backlog(
    target: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
) -> dict:
    """List reviewable eval backlog drafts without promoting them into eval YAML."""
    items = get_feedback_service().list_eval_backlog(
        target=target,
        review_status=review_status,
    )
    return {"items": items, "summary": summarize_eval_backlog(items)}
