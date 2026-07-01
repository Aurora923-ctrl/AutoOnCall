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


def get_approval_service() -> ApprovalService:
    """Return the approval service singleton."""
    return approval_service


@router.get(
    "/approvals/pending",
    response_model=ApprovalListResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def list_pending_approvals(incident_id: str | None = Query(default=None)) -> dict:
    """List pending approval requests."""
    requests = get_approval_service().list_pending(incident_id=incident_id)
    return {"items": [request.model_dump(mode="json") for request in requests]}


@router.get(
    "/incidents/{incident_id}/approval",
    response_model=IncidentApprovalListResponse,
    dependencies=[Depends(require_scope(READ_SCOPE))],
)
async def list_incident_approvals(
    incident_id: str,
    status: Literal["pending", "approved", "rejected", "cancelled"] | None = Query(default=None),
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
