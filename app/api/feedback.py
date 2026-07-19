"""Feedback APIs for RAG/AIOps bad-case regression loops."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.auth import ADMIN_SCOPE, DIAGNOSE_SCOPE, READ_SCOPE, AuthPrincipal, require_scope
from app.models.api_contracts import (
    BadCaseFeedbackListResponse,
    BadCaseFeedbackResponse,
    EvalBacklogListResponse,
)
from app.models.feedback import BadCaseFeedbackCreate
from app.services.aiops_store import create_aiops_store
from app.services.feedback_service import FeedbackService, feedback_service, summarize_eval_backlog

router = APIRouter()


def get_feedback_service() -> FeedbackService:
    """Return the feedback service singleton."""
    return feedback_service


@router.post(
    "/feedback",
    response_model=BadCaseFeedbackResponse,
)
async def submit_bad_case_feedback(
    payload: BadCaseFeedbackCreate,
    principal: AuthPrincipal = Depends(require_scope(DIAGNOSE_SCOPE)),
) -> dict:
    """Submit thumb feedback with runtime context for eval-case drafting."""
    feedback = get_feedback_service().submit_bad_case_feedback(
        payload,
        owner_id=_feedback_owner_id(principal),
        reference_store=create_aiops_store(),
    )
    return {"feedback": feedback}


@router.get(
    "/feedback/bad-cases",
    response_model=BadCaseFeedbackListResponse,
)
async def list_bad_case_feedback(
    target: str | None = Query(default=None),
    high_value_only: bool = Query(default=False),
    principal: AuthPrincipal = Depends(require_scope(READ_SCOPE)),
) -> dict:
    """List captured bad cases that can be promoted into regression cases."""
    return {
        "items": get_feedback_service().list_bad_cases(
            target=target,
            high_value_only=high_value_only,
            owner_id=_feedback_owner_filter(principal),
            reference_store=create_aiops_store(),
        )
    }


@router.get(
    "/feedback/eval-backlog",
    response_model=EvalBacklogListResponse,
)
async def list_eval_backlog(
    target: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
    principal: AuthPrincipal = Depends(require_scope(READ_SCOPE)),
) -> dict:
    """List reviewable eval backlog drafts without promoting them into eval YAML."""
    items = get_feedback_service().list_eval_backlog(
        target=target,
        review_status=review_status,
        owner_id=_feedback_owner_filter(principal),
        reference_store=create_aiops_store(),
    )
    return {"items": items, "summary": summarize_eval_backlog(items)}


def _feedback_owner_filter(principal: AuthPrincipal) -> str | None:
    """Scope feedback reads to the caller unless it is an administrator."""
    return None if principal.has_scope(ADMIN_SCOPE) else _feedback_owner_id(principal)


def _feedback_owner_id(principal: AuthPrincipal) -> str:
    """Use credential identity rather than a caller-configurable display name."""
    return principal.principal_id if principal.enabled else "anonymous"
