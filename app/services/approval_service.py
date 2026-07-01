"""Storage service for AIOps human approval requests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from loguru import logger

from app.models.approval import ApprovalRequest
from app.models.incident import utc_now
from app.services.aiops_store import create_aiops_store
from app.services.incident_state_builder import build_incident_state_from_approval
from app.services.legacy_migration import resolve_legacy_jsonl_path
from app.services.sqlite_store import resolve_sqlite_path
from app.services.trace_service import trace_service


class ApprovalNotFoundError(KeyError):
    """Raised when an approval request cannot be found."""


class ApprovalStateError(ValueError):
    """Raised when an approval request cannot transition state."""


class ApprovalService:
    """Approval repository backed by SQLite."""

    def __init__(
        self,
        storage_path: str | Path | None = None,
        *,
        legacy_storage_path: str | Path | None = None,
        sync_report_status: bool | None = None,
    ):
        raw_storage_path = Path(storage_path) if storage_path is not None else None
        self.database_path = resolve_sqlite_path(raw_storage_path)
        self._store = create_aiops_store(raw_storage_path)
        self.storage_path = getattr(self._store, "storage_path", self.database_path)
        self._sync_report_status = (
            storage_path is None if sync_report_status is None else sync_report_status
        )
        self._migrate_legacy_jsonl(
            legacy_storage_path
            if legacy_storage_path is not None
            else resolve_legacy_jsonl_path(raw_storage_path, "approvals.jsonl")
        )

    def create_request(self, request: ApprovalRequest) -> ApprovalRequest:
        """Create or replace an approval request and persist it."""
        self._store.save_approval_request(request)
        self._store.save_incident_state(
            build_incident_state_from_approval(
                approval=request,
                status="waiting_approval",
                status_reason="Approval request created",
            )
        )
        self._record_trace_event("approval_request", request, request.status, request.reason)
        return request

    def get_request(self, approval_id: str) -> ApprovalRequest:
        """Return a single approval request."""
        request = self._store.get_approval_request(approval_id)
        if not request:
            raise ApprovalNotFoundError(approval_id)
        return request

    def list_requests(
        self,
        incident_id: str | None = None,
        status: Literal["pending", "approved", "rejected", "cancelled"] | None = None,
    ) -> list[ApprovalRequest]:
        """List approval requests, optionally filtered by incident and status."""
        return self._store.list_approval_requests(incident_id=incident_id, status=status)

    def list_pending(self, incident_id: str | None = None) -> list[ApprovalRequest]:
        """List pending approval requests."""
        return self.list_requests(incident_id=incident_id, status="pending")

    def decide_request(
        self,
        approval_id: str,
        decision: Literal["approve", "reject"],
        decided_by: str = "operator",
        reason: str = "",
    ) -> ApprovalRequest:
        """Approve or reject a pending request."""
        request = self.get_request(approval_id)
        if request.status != "pending":
            raise ApprovalStateError(f"Approval {approval_id} is already {request.status}")

        status: Literal["approved", "rejected"] = (
            "approved" if decision == "approve" else "rejected"
        )
        change_plan = request.change_plan
        metadata = dict(request.metadata or {})
        if change_plan is not None:
            plan_status = "approved" if status == "approved" else "rejected"
            change_plan = change_plan.model_copy(update={"status": plan_status})
            metadata["change_plan"] = change_plan.model_dump(mode="json")
        updated = request.model_copy(
            update={
                "status": status,
                "decided_at": utc_now(),
                "decided_by": decided_by,
                "decision_reason": reason,
                "change_plan": change_plan,
                "metadata": metadata,
            }
        )
        if not self._store.save_approval_decision_if_pending(updated):
            latest = self.get_request(approval_id)
            raise ApprovalStateError(f"Approval {approval_id} is already {latest.status}")
        self._store.save_incident_state(
            build_incident_state_from_approval(
                approval=updated,
                status=f"approval_{status}",
                status_reason=reason or f"Approval {status}",
            )
        )
        self._record_trace_event("approval_decision", updated, status, reason)
        self._sync_latest_report_decision(updated)
        return updated

    def decide_latest_pending(
        self,
        incident_id: str,
        decision: Literal["approve", "reject"],
        decided_by: str = "operator",
        reason: str = "",
    ) -> ApprovalRequest:
        """Decide the newest pending request for an incident."""
        pending = self.list_pending(incident_id=incident_id)
        if not pending:
            raise ApprovalNotFoundError(f"No pending approval for incident {incident_id}")
        return self.decide_request(
            approval_id=pending[-1].approval_id,
            decision=decision,
            decided_by=decided_by,
            reason=reason,
        )

    def _migrate_legacy_jsonl(self, legacy_storage_path: str | Path | None) -> None:
        if legacy_storage_path is None:
            return
        path = Path(legacy_storage_path)
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                payload = record.get("approval") or record
                request = ApprovalRequest.model_validate(payload)
            except Exception:
                continue
            self._store.save_approval_request(request)

    def _record_trace_event(
        self,
        event_type: str,
        request: ApprovalRequest,
        status: str,
        reason: str = "",
    ) -> None:
        """Write approval lifecycle changes into the trace stream."""
        trace_id = str(request.metadata.get("trace_id") or "trace-unknown")
        trace_service.record_approval_event(
            trace_id=trace_id,
            incident_id=request.incident_id,
            approval_id=request.approval_id,
            event_type=event_type,
            action=request.action,
            status=status,
            reason=reason or request.reason,
            step_id=request.step_id,
            metadata={
                "tool_name": request.tool_name,
                "risk_level": request.risk_level,
                "decided_by": request.decided_by,
                "decision_reason": request.decision_reason,
            },
        )

    def _sync_latest_report_decision(self, request: ApprovalRequest) -> None:
        """Best-effort update for the latest incident report after approval decisions."""
        if not self._sync_report_status:
            return
        try:
            from app.services.report_generator import report_generator

            report_generator.mark_approval_decided(
                incident_id=request.incident_id,
                approval_status=request.status,
                decided_by=request.decided_by,
                reason=request.decision_reason,
                approval_request=request.model_dump(mode="json"),
            )
        except Exception as exc:
            # Approval persistence is the source of truth; report synchronization must not
            # make an operator decision fail.
            logger.warning(
                "Approval report synchronization failed: incident_id={}, approval_id={}, error={}",
                request.incident_id,
                request.approval_id,
                exc,
            )
            return


approval_service = ApprovalService()
