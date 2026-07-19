"""Storage service for AIOps human approval requests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from loguru import logger

from app.models.approval import ApprovalRequest
from app.models.change_plan import change_plan_fingerprint
from app.models.incident import utc_now
from app.services.aiops_store import create_aiops_store
from app.services.incident_state_builder import build_incident_state_from_approval
from app.services.legacy_migration import resolve_legacy_jsonl_path
from app.services.sqlite_store import resolve_sqlite_path
from app.services.trace_service import TraceService, trace_service


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
        trace_repository: TraceService | None = None,
    ):
        raw_storage_path = Path(storage_path) if storage_path is not None else None
        self.database_path = resolve_sqlite_path(raw_storage_path)
        self._store = create_aiops_store(raw_storage_path)
        self.storage_path = getattr(self._store, "storage_path", self.database_path)
        self._sync_report_status = (
            storage_path is None if sync_report_status is None else sync_report_status
        )
        self._configured_trace_service = trace_repository
        self._module_trace_service_at_init = trace_service
        self._local_trace_service = (
            TraceService(raw_storage_path) if raw_storage_path is not None else trace_service
        )
        self._migrate_legacy_jsonl(
            legacy_storage_path
            if legacy_storage_path is not None
            else resolve_legacy_jsonl_path(raw_storage_path, "approvals.jsonl")
        )

    def create_request(self, request: ApprovalRequest) -> ApprovalRequest:
        """Create a pending approval request without mutating an existing request."""
        if request.status != "pending":
            raise ApprovalStateError("new approval requests must start in pending status")
        request = self._with_plan_fingerprint(request).model_copy(
            update={"projection_pending": ["incident_state", "trace"]}
        )
        try:
            stored, created = self._store.create_approval_request_once(
                request,
                idempotency_key=f"approval-id:{request.approval_id}",
            )
        except ValueError as exc:
            raise ApprovalStateError(str(exc)) from exc
        if not created:
            return stored

        return self._sync_committed_creation(stored)

    def create_request_once(
        self,
        request: ApprovalRequest,
        *,
        idempotency_key: str,
    ) -> ApprovalRequest:
        """Atomically create one pending request for an idempotent risky action."""
        if request.status != "pending":
            raise ApprovalStateError("new approval requests must start in pending status")
        key = idempotency_key.strip()
        if not key:
            raise ApprovalStateError("approval idempotency key is required")

        request = self._with_plan_fingerprint(request)
        metadata = dict(request.metadata or {})
        existing_key = str(metadata.get("idempotency_key") or "")
        if existing_key and existing_key != key:
            raise ApprovalStateError("approval idempotency key does not match request metadata")
        metadata["idempotency_key"] = key
        request = request.model_copy(
            update={
                "metadata": metadata,
                "projection_pending": ["incident_state", "trace"],
            }
        )

        try:
            stored, created = self._store.create_approval_request_once(
                request,
                idempotency_key=key,
            )
        except ValueError as exc:
            raise ApprovalStateError(str(exc)) from exc
        if not created:
            return stored

        return self._sync_committed_creation(stored)

    def get_request(self, approval_id: str) -> ApprovalRequest:
        """Return a single approval request."""
        request = self._store.get_approval_request(approval_id)
        if not request:
            raise ApprovalNotFoundError(approval_id)
        return self._repair_request_if_needed(request)

    def list_requests(
        self,
        incident_id: str | None = None,
        status: Literal["pending", "approved", "rejected", "cancelled"] | None = None,
    ) -> list[ApprovalRequest]:
        """List approval requests, optionally filtered by incident and status."""
        return [
            self._repair_request_if_needed(request)
            for request in self._store.list_approval_requests(
                incident_id=incident_id,
                status=status,
            )
        ]

    def list_pending(self, incident_id: str | None = None) -> list[ApprovalRequest]:
        """List active pending requests and cancel expired authorizations."""
        active: list[ApprovalRequest] = []
        for request in self.list_requests(incident_id=incident_id, status="pending"):
            if self.is_expired(request):
                try:
                    self._cancel_pending_request(
                        request,
                        decided_by="system",
                        reason="approval expired while queued",
                    )
                except ApprovalStateError:
                    pass
                continue
            active.append(request)
        return active

    def has_unstarted_change_action(self, request: ApprovalRequest) -> bool:
        """Return whether an approved plan has not created any execution record."""
        if request.status != "approved" or request.change_plan is None or self.is_expired(request):
            return False
        executions = self._store.list_change_executions(
            incident_id=request.incident_id,
            change_plan_id=request.change_plan.change_plan_id,
        )
        return not any(execution.approval_id == request.approval_id for execution in executions)

    def decide_request(
        self,
        approval_id: str,
        decision: Literal["approve", "reject", "cancel"],
        decided_by: str = "operator",
        reason: str = "",
    ) -> ApprovalRequest:
        """Approve or reject a pending request."""
        request = self.get_request(approval_id)
        if request.status != "pending":
            raise ApprovalStateError(f"Approval {approval_id} is already {request.status}")
        if self.is_expired(request):
            self._cancel_pending_request(
                request,
                decided_by="system",
                reason="approval expired before decision",
            )
            raise ApprovalStateError(f"Approval {approval_id} expired and was cancelled")
        self._assert_plan_fingerprint(request)

        status = cast(
            Literal["approved", "rejected", "cancelled"],
            {
                "approve": "approved",
                "reject": "rejected",
                "cancel": "cancelled",
            }[decision],
        )
        change_plan = request.change_plan
        metadata = dict(request.metadata or {})
        if change_plan is not None:
            change_plan = change_plan.model_copy(update={"status": status})
            metadata["change_plan"] = change_plan.model_dump(mode="json")
            metadata["change_plan_fingerprint"] = change_plan_fingerprint(change_plan)
        updated = request.model_copy(
            update={
                "status": status,
                "decided_at": utc_now(),
                "decided_by": decided_by,
                "decision_reason": reason,
                "change_plan": change_plan,
                "metadata": metadata,
                "projection_pending": self._decision_projection_names(),
            }
        )
        if not self._store.save_approval_decision_if_pending(updated):
            latest = self.get_request(approval_id)
            raise ApprovalStateError(f"Approval {approval_id} is already {latest.status}")
        return self._sync_committed_decision(updated, reason=reason)

    def cancel_request(
        self,
        approval_id: str,
        *,
        decided_by: str = "operator",
        reason: str = "",
    ) -> ApprovalRequest:
        """Cancel one pending approval so it can no longer be reused."""
        return self.decide_request(
            approval_id,
            decision="cancel",
            decided_by=decided_by,
            reason=reason,
        )

    @staticmethod
    def is_expired(request: ApprovalRequest, *, now: datetime | None = None) -> bool:
        """Return whether a pending approval has exceeded its authorization window."""
        created_at = request.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        current = now or utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return (current - created_at).total_seconds() > request.expires_in_seconds

    def _cancel_pending_request(
        self,
        request: ApprovalRequest,
        *,
        decided_by: str,
        reason: str,
    ) -> ApprovalRequest:
        change_plan = request.change_plan
        metadata = dict(request.metadata or {})
        if change_plan is not None:
            change_plan = change_plan.model_copy(update={"status": "cancelled"})
            metadata["change_plan"] = change_plan.model_dump(mode="json")
            metadata["change_plan_fingerprint"] = change_plan_fingerprint(change_plan)
        updated = request.model_copy(
            update={
                "status": "cancelled",
                "decided_at": utc_now(),
                "decided_by": decided_by,
                "decision_reason": reason,
                "change_plan": change_plan,
                "metadata": metadata,
                "projection_pending": self._decision_projection_names(),
            }
        )
        if not self._store.save_approval_decision_if_pending(updated):
            latest = self.get_request(request.approval_id)
            raise ApprovalStateError(f"Approval {request.approval_id} is already {latest.status}")
        return self._sync_committed_decision(updated, reason=reason)

    def _sync_committed_decision(
        self,
        request: ApprovalRequest,
        *,
        reason: str,
    ) -> ApprovalRequest:
        pending = self._sync_decision_projections(request, reason=reason)
        if not self._sync_latest_report_decision(request):
            pending.append("report")
        updated = request.model_copy(update={"projection_pending": sorted(set(pending))})
        if updated.projection_pending != request.projection_pending:
            self._store.save_approval_request(updated)
        return updated

    def _decision_projection_names(self) -> list[str]:
        pending = ["incident_state", "trace"]
        if self._sync_report_status:
            pending.append("report")
        return pending

    def _sync_committed_creation(self, request: ApprovalRequest) -> ApprovalRequest:
        pending = self._sync_creation_projections(request)
        updated = request.model_copy(update={"projection_pending": sorted(set(pending))})
        if updated.projection_pending != request.projection_pending:
            self._store.save_approval_request(updated)
        return updated

    def _sync_decision_projections(
        self,
        request: ApprovalRequest,
        *,
        reason: str,
    ) -> list[str]:
        """Update rebuildable projections and return any failed projection names."""
        pending: list[str] = []
        try:
            self._store.save_incident_state(
                build_incident_state_from_approval(
                    approval=request,
                    status=f"approval_{request.status}",
                    status_reason=reason or f"Approval {request.status}",
                )
            )
        except Exception as exc:
            pending.append("incident_state")
            logger.warning(
                "Approval incident-state projection failed: approval_id={}, error={}",
                request.approval_id,
                exc,
            )
        try:
            self._record_trace_event(
                "approval_decision",
                request,
                request.status,
                reason,
            )
        except Exception as exc:
            pending.append("trace")
            logger.warning(
                "Approval trace projection failed: approval_id={}, error={}",
                request.approval_id,
                exc,
            )
        return pending

    def repair_pending_projections(self, approval_id: str) -> ApprovalRequest:
        """Retry projections previously marked as incomplete."""
        request = self._store.get_approval_request(approval_id)
        if request is None:
            raise ApprovalNotFoundError(approval_id)
        if not request.projection_pending:
            return request
        if request.status == "pending":
            return self._sync_committed_creation(
                request.model_copy(update={"projection_pending": []})
            )
        return self._sync_committed_decision(
            request.model_copy(update={"projection_pending": []}),
            reason=request.decision_reason or request.reason,
        )

    def _repair_request_if_needed(self, request: ApprovalRequest) -> ApprovalRequest:
        if not request.projection_pending:
            return request
        return self.repair_pending_projections(request.approval_id)

    def _sync_creation_projections(self, request: ApprovalRequest) -> list[str]:
        """Update request projections after the approval fact has committed."""
        pending: list[str] = []
        try:
            self._store.save_incident_state(
                build_incident_state_from_approval(
                    approval=request,
                    status="waiting_approval",
                    status_reason="Approval request created",
                )
            )
        except Exception as exc:
            pending.append("incident_state")
            logger.warning(
                "Approval request incident-state projection failed: approval_id={}, error={}",
                request.approval_id,
                exc,
            )
        try:
            self._record_trace_event(
                "approval_request",
                request,
                request.status,
                request.reason,
            )
        except Exception as exc:
            pending.append("trace")
            logger.warning(
                "Approval request trace projection failed: approval_id={}, error={}",
                request.approval_id,
                exc,
            )
        return pending

    @staticmethod
    def _with_plan_fingerprint(request: ApprovalRequest) -> ApprovalRequest:
        if request.change_plan is None:
            return request
        metadata = dict(request.metadata or {})
        fingerprint = change_plan_fingerprint(request.change_plan)
        existing = str(metadata.get("change_plan_fingerprint") or "")
        if existing and existing != fingerprint:
            raise ApprovalStateError("approval change plan fingerprint does not match plan content")
        metadata["change_plan_fingerprint"] = fingerprint
        metadata["change_plan"] = request.change_plan.model_dump(mode="json")
        return request.model_copy(update={"metadata": metadata})

    @staticmethod
    def _assert_plan_fingerprint(request: ApprovalRequest) -> None:
        if request.change_plan is None:
            return
        expected = str(request.metadata.get("change_plan_fingerprint") or "")
        actual = change_plan_fingerprint(request.change_plan)
        if not expected or expected != actual:
            raise ApprovalStateError("approval change plan content changed after request creation")

    def decide_latest_pending(
        self,
        incident_id: str,
        decision: Literal["approve", "reject", "cancel"],
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
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                payload = record.get("approval") or record
                request = ApprovalRequest.model_validate(payload)
            except Exception as exc:
                logger.warning(
                    "Skipping invalid legacy approval record: path={}, line={}, error={}",
                    path,
                    line_number,
                    exc,
                )
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
        self._effective_trace_service().record_approval_event(
            event_id=_approval_projection_event_id(event_type, request),
            created_at=request.decided_at or request.created_at,
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

    def _effective_trace_service(self) -> TraceService:
        """Resolve explicit, test-overridden, or same-store trace persistence."""
        if self._configured_trace_service is not None:
            return self._configured_trace_service
        if trace_service is not self._module_trace_service_at_init:
            return trace_service
        return self._local_trace_service

    def _sync_latest_report_decision(self, request: ApprovalRequest) -> bool:
        """Best-effort update for the latest incident report after approval decisions."""
        if not self._sync_report_status:
            return True
        try:
            from app.services.report_generator import report_generator

            report_generator.mark_approval_decided(
                incident_id=request.incident_id,
                approval_status=request.status,
                decided_by=request.decided_by,
                reason=request.decision_reason,
                approval_request=request.model_dump(mode="json"),
            )
            return True
        except Exception as exc:
            # Approval persistence is the source of truth; report synchronization must not
            # make an operator decision fail.
            logger.warning(
                "Approval report synchronization failed: incident_id={}, approval_id={}, error={}",
                request.incident_id,
                request.approval_id,
                exc,
            )
            return False


approval_service = ApprovalService()


def _approval_projection_event_id(event_type: str, request: ApprovalRequest) -> str:
    timestamp = request.decided_at or request.created_at
    suffix = timestamp.isoformat().replace("+00:00", "Z")
    return f"approval:{event_type}:{request.approval_id}:{suffix}"[:128]
