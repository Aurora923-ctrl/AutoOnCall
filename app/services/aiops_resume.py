"""Approval resume use case for AIOps workflows."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, Protocol

from app.models.approval import ApprovalRequest


class ApprovalResumeOwner(Protocol):
    async def _execute_approval_resume(
        self,
        *,
        session_id: str,
        incident_id: str,
        approval: ApprovalRequest,
    ) -> AsyncGenerator[dict[str, Any], None]: ...


class AIOpsResume:
    """Expose the complete approval-resume use case behind one boundary."""

    def __init__(self, owner: ApprovalResumeOwner):
        self._owner = owner

    def execute(
        self,
        *,
        session_id: str,
        incident_id: str,
        approval: ApprovalRequest,
    ) -> AsyncGenerator[dict[str, Any], None]:
        return self._owner._execute_approval_resume(
            session_id=session_id,
            incident_id=incident_id,
            approval=approval,
        )
