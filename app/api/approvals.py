"""Human approval API for risky AIOps actions."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import APPROVE_SCOPE, READ_SCOPE, AuthPrincipal, audit_actor, require_scope
from app.models.api_contracts import (
    ApprovalDecisionResponse,
    ApprovalListResponse,
    IncidentApprovalListResponse,
)
from app.models.approval import ApprovalDecisionRequest
from app.services.approval_service import (
    ApprovalNotFoundError,
    ApprovalService,
    ApprovalStateError,
    approval_service,
)

router = APIRouter()
ApprovalStatus = Literal["pending", "approved", "rejected", "cancelled"]


def get_approval_service() -> ApprovalService:
    """Return the approval service singleton."""
    return approval_service


@router.get(
    "/approvals/pending",
    response_model=ApprovalListResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def list_pending_approvals(
    incident_id: str | None = Query(default=None),
    include_approved_actions: bool = Query(default=False),
) -> dict:
    """List the operator approval queue."""
    incident_id = incident_id if isinstance(incident_id, str) else None
    include_approved_actions = (
        include_approved_actions
        if isinstance(include_approved_actions, bool)
        else False
    )
    service = get_approval_service()
    requests = service.list_pending(incident_id=incident_id)
    if include_approved_actions:
        approved_requests = service.list_requests(incident_id=incident_id, status="approved")
        requests = [
            *requests,
            *[
                request
                for request in approved_requests
                if _approval_has_next_action(request)
            ],
        ]
    return {"items": [request.model_dump(mode="json") for request in requests]}


@router.get(
    "/incidents/{incident_id}/approval",
    response_model=IncidentApprovalListResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def list_incident_approvals(
    incident_id: str,
    status: ApprovalStatus | None = Query(default=None),
) -> dict:
    """List approval requests for one incident."""
    requests = get_approval_service().list_requests(incident_id=incident_id, status=status)
    return {
        "incident_id": incident_id,
        "items": [request.model_dump(mode="json") for request in requests],
    }


@router.post(
    "/incidents/{incident_id}/approval",
    response_model=ApprovalDecisionResponse,
)
async def submit_incident_approval(
    incident_id: str,
    request: ApprovalDecisionRequest,
    principal: AuthPrincipal = Depends(require_scope(APPROVE_SCOPE)),
) -> dict:
    """Approve or reject the latest pending request for an incident."""
    decided_by = audit_actor(principal, request.decided_by)
    try:
        if request.approval_id:
            existing = get_approval_service().get_request(request.approval_id)
            if existing.incident_id != incident_id:
                raise HTTPException(
                    status_code=400,
                    detail="approval_id does not belong to the requested incident",
                )
            approval = get_approval_service().decide_request(
                approval_id=request.approval_id,
                decision=request.decision,
                decided_by=decided_by,
                reason=request.reason,
            )
        else:
            approval = get_approval_service().decide_latest_pending(
                incident_id=incident_id,
                decision=request.decision,
                decided_by=decided_by,
                reason=request.reason,
            )
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"approval": approval.model_dump(mode="json")}


def _approval_has_next_action(request: object) -> bool:
    status = getattr(request, "status", "")
    if status != "approved":
        return False
    change_plan = getattr(request, "change_plan", None)
    return bool(getattr(request, "approval_id", "")) or bool(
        getattr(change_plan, "change_plan_id", "")
    )
