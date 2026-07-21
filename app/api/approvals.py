"""Human approval API for risky AIOps actions."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.core.auth import APPROVE_SCOPE, READ_SCOPE, AuthPrincipal, audit_actor, require_scope
from app.core.ownership import owns_approval
from app.models.api_contracts import (
    ApprovalDecisionResponse,
    ApprovalListResponse,
    IncidentApprovalListResponse,
)
from app.models.approval import ApprovalDecisionRequest, ApprovalRequest
from app.services.approval_service import (
    ApprovalNotFoundError,
    ApprovalService,
    ApprovalStateError,
    approval_service,
)

router = APIRouter()
ApprovalStatus = Literal["pending", "approved", "rejected", "cancelled"]
RESOURCE_ID_MAX_LENGTH = 128
IncidentId = Annotated[str, Path(..., min_length=1, max_length=RESOURCE_ID_MAX_LENGTH)]


def get_approval_service() -> ApprovalService:
    """Return the approval service singleton."""
    return approval_service


@router.get(
    "/approvals/pending",
    response_model=ApprovalListResponse,
)
async def list_pending_approvals(
    incident_id: str | None = Query(default=None, min_length=1, max_length=RESOURCE_ID_MAX_LENGTH),
    include_approved_actions: bool = Query(default=False),
    principal: AuthPrincipal = Depends(require_scope(READ_SCOPE)),
) -> dict:
    """List the operator approval queue."""
    incident_id = incident_id if isinstance(incident_id, str) else None
    include_approved_actions = (
        include_approved_actions if isinstance(include_approved_actions, bool) else False
    )
    service = get_approval_service()
    requests = [
        request
        for request in service.list_pending(incident_id=incident_id)
        if owns_approval(principal, request)
    ]
    if include_approved_actions:
        approved_requests = [
            request
            for request in service.list_requests(incident_id=incident_id, status="approved")
            if owns_approval(principal, request)
        ]
        requests = [
            *requests,
            *[
                request
                for request in approved_requests
                if _approval_has_next_action(service, request)
            ],
        ]
    return {"items": [request.model_dump(mode="json") for request in requests]}


@router.get(
    "/incidents/{incident_id}/approval",
    response_model=IncidentApprovalListResponse,
)
async def list_incident_approvals(
    incident_id: IncidentId,
    status: ApprovalStatus | None = Query(default=None),
    principal: AuthPrincipal = Depends(require_scope(READ_SCOPE)),
) -> dict:
    """List approval requests for one incident."""
    requests = [
        request
        for request in get_approval_service().list_requests(incident_id=incident_id, status=status)
        if owns_approval(principal, request)
    ]
    return {
        "incident_id": incident_id,
        "items": [request.model_dump(mode="json") for request in requests],
    }


@router.post(
    "/incidents/{incident_id}/approval",
    response_model=ApprovalDecisionResponse,
)
async def submit_incident_approval(
    incident_id: IncidentId,
    request: ApprovalDecisionRequest,
    principal: AuthPrincipal = Depends(require_scope(APPROVE_SCOPE)),
) -> dict:
    """Approve or reject the explicitly identified request for an incident."""
    decided_by = audit_actor(principal, request.decided_by)
    try:
        existing = get_approval_service().get_request(request.approval_id)
        if existing.incident_id != incident_id:
            raise HTTPException(
                status_code=400,
                detail="approval_id does not belong to the requested incident",
            )
        if not owns_approval(principal, existing):
            raise HTTPException(status_code=404, detail="approval not found")
        approval = get_approval_service().decide_request(
            approval_id=request.approval_id,
            decision=request.decision,
            decided_by=decided_by,
            decided_by_principal_id=principal.principal_id,
            reason=request.reason,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"approval": approval.model_dump(mode="json")}


def _approval_has_next_action(service: ApprovalService, request: ApprovalRequest) -> bool:
    if request.status != "approved":
        return False
    if request.change_plan is None or not request.change_plan.change_plan_id:
        return False
    return service.has_unstarted_change_action(request)
