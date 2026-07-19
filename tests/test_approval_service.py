"""Tests for the local human approval service."""

import importlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app.models.approval import ApprovalRequest
from app.models.change_plan import ChangePlan
from app.models.incident import utc_now
from app.models.report import DiagnosisReport
from app.services.approval_service import ApprovalService, ApprovalStateError
from app.services.approval_workflow import create_approval_request_from_risk_decision
from app.services.report_generator import ReportGenerator


def test_approval_service_creates_lists_and_persists_pending_request(tmp_path) -> None:
    database_path = tmp_path / "approvals.db"
    service = ApprovalService(database_path)
    request = ApprovalRequest(
        incident_id="inc-1",
        action="重启生产服务",
        risk_level="high",
        reason="会影响线上流量",
        step_id="s1",
        tool_name="restart_service",
    )

    created = service.create_request(request)

    assert created.status == "pending"
    assert service.get_request(created.approval_id).action == "重启生产服务"
    assert service.list_pending("inc-1")[0].approval_id == created.approval_id

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT payload FROM approval_requests WHERE approval_id = ?",
            (created.approval_id,),
        ).fetchone()

    assert row is not None
    assert json.loads(row[0])["approval_id"] == created.approval_id

    reloaded = ApprovalService(database_path)
    assert reloaded.get_request(created.approval_id).action == "重启生产服务"


def test_approval_service_approves_latest_pending_request(tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db")
    first = service.create_request(
        ApprovalRequest(incident_id="inc-2", action="限流接口", risk_level="medium")
    )
    second = service.create_request(
        ApprovalRequest(incident_id="inc-2", action="重启服务", risk_level="high")
    )

    approved = service.decide_latest_pending(
        incident_id="inc-2",
        decision="approve",
        decided_by="oncall",
        reason="已确认变更窗口",
    )

    assert approved.approval_id == second.approval_id
    assert approved.status == "approved"
    assert approved.decided_by == "oncall"
    assert approved.decision_reason == "已确认变更窗口"
    assert service.get_request(first.approval_id).status == "pending"


@pytest.mark.asyncio
async def test_pending_approval_api_can_include_approved_followups(monkeypatch, tmp_path) -> None:
    approvals_api = importlib.import_module("app.api.approvals")
    service = ApprovalService(tmp_path / "approvals.db")
    pending = service.create_request(
        ApprovalRequest(incident_id="inc-queue", action="新变更", risk_level="high")
    )
    approved_without_change = service.create_request(
        ApprovalRequest(incident_id="inc-queue", action="已批准诊断", risk_level="medium")
    )
    service.decide_request(
        approved_without_change.approval_id,
        decision="approve",
        decided_by="sre",
        reason="窗口已确认",
    )
    approved_with_change = service.create_request(
        ApprovalRequest(
            incident_id="inc-queue",
            action="已批准变更",
            risk_level="medium",
            change_plan=ChangePlan(
                incident_id="inc-queue",
                action="调整 Redis maxclients",
            ),
        )
    )
    approved_with_change = service.decide_request(
        approved_with_change.approval_id,
        decision="approve",
        decided_by="sre",
        reason="窗口已确认",
    )
    monkeypatch.setattr(approvals_api, "get_approval_service", lambda: service)

    default_payload = await approvals_api.list_pending_approvals()
    followup_payload = await approvals_api.list_pending_approvals(
        include_approved_actions=True,
    )

    assert [item["approval_id"] for item in default_payload["items"]] == [pending.approval_id]
    assert [item["approval_id"] for item in followup_payload["items"]] == [
        pending.approval_id,
        approved_with_change.approval_id,
    ]

    change_service = importlib.import_module(
        "app.services.change_execution_service"
    ).ChangeExecutionService(
        service.database_path,
        approval_repository=service,
    )
    await _collect_change_events(
        change_service,
        incident_id=approved_with_change.incident_id,
        change_plan_id=approved_with_change.change_plan.change_plan_id,
        approval_id=approved_with_change.approval_id,
    )
    filtered_payload = await approvals_api.list_pending_approvals(
        include_approved_actions=True,
    )
    assert [item["approval_id"] for item in filtered_payload["items"]] == [pending.approval_id]


async def _collect_change_events(service, **kwargs):
    return [event async for event in service.start_after_approval(**kwargs)]


def test_approval_workflow_reuses_same_pending_request_by_idempotency_key(tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db")
    state = {
        "session_id": "session-dup",
        "trace_id": "trace-dup",
        "incident": {
            "incident_id": "inc-dup",
            "service_name": "checkout-service",
            "environment": "prod",
        },
    }
    decision = {
        "action": "重启 checkout-service",
        "risk_level": "high",
        "policy": "approval_required",
        "step_id": "s-risk",
        "tool_name": "restart_service",
        "reason": "生产写操作需要审批",
    }

    first = create_approval_request_from_risk_decision(
        state,
        decision,
        approval_repository=service,
    )
    second = create_approval_request_from_risk_decision(
        state,
        decision,
        approval_repository=service,
    )

    assert second.approval_id == first.approval_id
    assert len(service.list_pending("inc-dup")) == 1
    assert first.metadata["idempotency_key"]

    service.decide_request(first.approval_id, decision="reject", decided_by="sre")
    third = create_approval_request_from_risk_decision(
        state,
        decision,
        approval_repository=service,
    )

    assert third.approval_id != first.approval_id
    assert len(service.list_requests(incident_id="inc-dup")) == 2


def test_approval_workflow_concurrent_creation_has_single_pending_request(tmp_path) -> None:
    database_path = tmp_path / "approval-concurrency.db"
    state = {
        "session_id": "session-concurrent",
        "trace_id": "trace-concurrent",
        "incident": {
            "incident_id": "inc-concurrent",
            "service_name": "checkout-service",
            "environment": "prod",
        },
    }
    decision = {
        "action": "restart checkout-service",
        "risk_level": "high",
        "policy": "approval_required",
        "step_id": "s-risk",
        "tool_name": "restart_service",
        "reason": "production write requires approval",
    }
    barrier = threading.Barrier(6)

    def create_request(_: int) -> ApprovalRequest:
        service = ApprovalService(database_path)
        barrier.wait()
        return create_approval_request_from_risk_decision(
            state,
            decision,
            approval_repository=service,
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        requests = list(executor.map(create_request, range(6)))

    assert len({request.approval_id for request in requests}) == 1
    assert len(ApprovalService(database_path).list_pending("inc-concurrent")) == 1


def test_approval_idempotency_is_scoped_to_run_environment_and_plan(tmp_path) -> None:
    service = ApprovalService(tmp_path / "approval-scope.db")
    decision = {
        "action": "restart checkout-service",
        "risk_level": "high",
        "policy": "approval_required",
        "step_id": "s-risk",
        "tool_name": "restart_service",
        "reason": "production write requires approval",
    }
    staging_state = {
        "session_id": "session-staging",
        "trace_id": "trace-staging",
        "incident": {
            "incident_id": "inc-scope",
            "service_name": "checkout-service",
            "environment": "staging",
        },
    }
    production_state = {
        "session_id": "session-production",
        "trace_id": "trace-production",
        "incident": {
            "incident_id": "inc-scope",
            "service_name": "checkout-service",
            "environment": "prod",
        },
    }

    staging = create_approval_request_from_risk_decision(
        staging_state,
        decision,
        approval_repository=service,
    )
    production = create_approval_request_from_risk_decision(
        production_state,
        decision,
        approval_repository=service,
    )

    assert staging.approval_id != production.approval_id
    assert staging.metadata["session_id"] == "session-staging"
    assert production.metadata["session_id"] == "session-production"
    assert staging.change_plan is not None
    assert production.change_plan is not None
    assert staging.change_plan.metadata["environment"] == "staging"
    assert production.change_plan.metadata["environment"] == "prod"


def test_approval_service_rejects_and_blocks_second_decision(tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db")
    request = service.create_request(
        ApprovalRequest(incident_id="inc-3", action="修改生产配置", risk_level="high")
    )

    rejected = service.decide_request(
        approval_id=request.approval_id,
        decision="reject",
        decided_by="sre",
        reason="缺少变更单",
    )

    assert rejected.status == "rejected"
    assert rejected.decided_by == "sre"

    with pytest.raises(ApprovalStateError):
        service.decide_request(request.approval_id, decision="approve")


def test_expired_pending_approval_is_cancelled_instead_of_approved(tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db")
    request = service.create_request(
        ApprovalRequest(
            incident_id="inc-expired",
            action="expired change",
            risk_level="high",
            expires_in_seconds=60,
            created_at=utc_now() - timedelta(minutes=5),
        )
    )

    with pytest.raises(ApprovalStateError, match="expired and was cancelled"):
        service.decide_request(request.approval_id, decision="approve", decided_by="sre")

    cancelled = service.get_request(request.approval_id)
    assert cancelled.status == "cancelled"
    assert cancelled.decided_by == "system"


def test_pending_queue_cancels_and_filters_expired_requests(tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db")
    expired = service.create_request(
        ApprovalRequest(
            incident_id="inc-expired-queue",
            action="expired queued change",
            risk_level="high",
            expires_in_seconds=60,
            created_at=utc_now() - timedelta(minutes=5),
        )
    )

    assert service.list_pending("inc-expired-queue") == []
    assert service.get_request(expired.approval_id).status == "cancelled"


def test_pending_approval_can_be_explicitly_cancelled(tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db")
    request = service.create_request(
        ApprovalRequest(
            incident_id="inc-cancel",
            action="cancel this change",
            risk_level="medium",
        )
    )

    cancelled = service.cancel_request(
        request.approval_id,
        decided_by="incident-commander",
        reason="context changed",
    )

    assert cancelled.status == "cancelled"
    assert cancelled.decision_reason == "context changed"
    with pytest.raises(ApprovalStateError):
        service.decide_request(request.approval_id, decision="approve")


def test_approval_decision_survives_projection_failures(monkeypatch, tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db", sync_report_status=False)
    request = service.create_request(
        ApprovalRequest(
            incident_id="inc-projection-failure",
            action="approve despite projection failure",
            risk_level="high",
        )
    )

    monkeypatch.setattr(
        service._store,
        "save_incident_state",
        lambda state: (_ for _ in ()).throw(RuntimeError("state unavailable")),
    )
    monkeypatch.setattr(
        service,
        "_record_trace_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("trace unavailable")),
    )

    approved = service.decide_request(
        request.approval_id,
        decision="approve",
        decided_by="sre",
    )

    assert approved.status == "approved"
    assert service.get_request(request.approval_id).status == "approved"


def test_approval_projection_failures_are_marked_and_repaired(monkeypatch, tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db", sync_report_status=False)
    request = service.create_request(
        ApprovalRequest(
            incident_id="inc-projection-repair",
            action="repair projections",
            risk_level="high",
            metadata={"trace_id": "trace-projection-repair"},
        )
    )
    original_save_state = service._store.save_incident_state
    original_record_trace = service._record_trace_event
    monkeypatch.setattr(
        service._store,
        "save_incident_state",
        lambda state: (_ for _ in ()).throw(RuntimeError("state unavailable")),
    )
    monkeypatch.setattr(
        service,
        "_record_trace_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("trace unavailable")),
    )

    approved = service.decide_request(
        request.approval_id,
        decision="approve",
        decided_by="sre",
    )

    assert approved.projection_pending == ["incident_state", "trace"]
    monkeypatch.setattr(service._store, "save_incident_state", original_save_state)
    monkeypatch.setattr(service, "_record_trace_event", original_record_trace)

    repaired = service.get_request(request.approval_id)
    assert repaired.projection_pending == []
    state = service._store.get_incident_state(request.incident_id)
    assert state is not None
    assert state.status == "approval_approved"
    assert (
        len(
            service._store.list_trace_events(
                incident_id=request.incident_id,
                event_type="approval_decision",
            )
        )
        == 1
    )


def test_approval_trace_projection_repairs_after_service_restart(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "approval-trace-restart.db"
    service = ApprovalService(database_path, sync_report_status=False)
    request = service.create_request(
        ApprovalRequest(
            incident_id="inc-trace-restart",
            action="repair trace after restart",
            risk_level="high",
            metadata={"trace_id": "trace-approval-restart"},
        )
    )
    monkeypatch.setattr(
        service,
        "_record_trace_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("trace unavailable")),
    )

    approved = service.decide_request(
        request.approval_id,
        decision="approve",
        decided_by="sre",
    )

    assert approved.projection_pending == ["trace"]

    reloaded = ApprovalService(database_path, sync_report_status=False)
    repaired = reloaded.get_request(request.approval_id)

    assert repaired.projection_pending == []
    events = reloaded._store.list_trace_events(
        incident_id=request.incident_id,
        event_type="approval_decision",
    )
    assert len(events) == 1
    assert events[0].trace_id == "trace-approval-restart"


def test_approval_decision_persists_projection_outbox_before_sync(monkeypatch, tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db", sync_report_status=False)
    request = service.create_request(
        ApprovalRequest(
            incident_id="inc-approval-outbox",
            action="persist projection repair intent",
            risk_level="high",
        )
    )
    monkeypatch.setattr(
        service,
        "_sync_committed_decision",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("process stopped")),
    )

    with pytest.raises(RuntimeError, match="process stopped"):
        service.decide_request(
            request.approval_id,
            decision="approve",
            decided_by="sre",
        )

    stored = service._store.get_approval_request(request.approval_id)
    assert stored is not None
    assert stored.status == "approved"
    assert stored.projection_pending == ["incident_state", "trace"]


def test_duplicate_approval_create_cannot_reopen_or_replace_decided_request(tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db")
    request = service.create_request(
        ApprovalRequest(
            approval_id="apr-stable-decision",
            incident_id="inc-stable-decision",
            action="manual mitigation",
            risk_level="high",
        )
    )
    approved = service.decide_request(
        request.approval_id,
        decision="approve",
        decided_by="sre",
    )

    with pytest.raises(ApprovalStateError, match="cannot be replaced"):
        service.create_request(
            request.model_copy(
                update={
                    "status": "pending",
                    "action": "conflicting action",
                }
            )
        )

    saved = service.get_request(request.approval_id)
    assert saved.status == "approved"
    assert saved.action == approved.action
    assert saved.decided_by == "sre"


def test_pending_approval_plan_cannot_be_replaced_before_decision(tmp_path) -> None:
    service = ApprovalService(tmp_path / "approvals.db")
    plan = ChangePlan(
        change_plan_id="chg-immutable",
        incident_id="inc-immutable",
        action="restart one service",
        execution_steps=["restart one service"],
    )
    request = service.create_request(
        ApprovalRequest(
            approval_id="apr-immutable",
            incident_id=plan.incident_id,
            action=plan.action,
            risk_level=plan.risk_level,
            change_plan=plan,
        )
    )
    replacement = plan.model_copy(update={"action": "restart all services"})

    with pytest.raises(ApprovalStateError, match="fingerprint|cannot be replaced"):
        service.create_request(request.model_copy(update={"change_plan": replacement}))

    saved = service.get_request(request.approval_id)
    assert saved.change_plan is not None
    assert saved.change_plan.action == "restart one service"
    assert saved.metadata["change_plan_fingerprint"]


def test_approval_service_syncs_report_lifecycle_on_decision(monkeypatch, tmp_path) -> None:
    report_generator_module = importlib.import_module("app.services.report_generator")
    generator = ReportGenerator(tmp_path / "reports.db")
    report = DiagnosisReport(
        incident_id="inc-4",
        trace_id="trace-4",
        status="waiting_approval",
        approval_status="pending",
        manual_action_required=True,
        approval_decision={
            "approval_id": "apr-4",
            "action": "重启生产 Pod",
            "status": "pending",
        },
        markdown="# pending report",
    )
    generator.save_report(report)
    monkeypatch.setattr(report_generator_module, "report_generator", generator)

    service = ApprovalService(tmp_path / "approvals.db", sync_report_status=True)
    request = service.create_request(
        ApprovalRequest(
            approval_id="apr-4",
            incident_id="inc-4",
            action="重启生产 Pod",
            risk_level="high",
            reason="生产操作需要审批",
            metadata={"trace_id": "trace-4"},
        )
    )

    service.decide_request(
        approval_id=request.approval_id,
        decision="reject",
        decided_by="sre",
        reason="缺少回滚方案",
    )

    updated = generator.get_report("inc-4")
    assert updated is not None
    assert updated.status == "approval_rejected"
    assert updated.approval_status == "rejected"
    assert updated.approval_decision["action"] == "重启生产 Pod"
    assert updated.approval_decision["decided_by"] == "sre"
    assert updated.approval_decision["decision_reason"] == "缺少回滚方案"
    assert "审批已拒绝" in updated.markdown
    assert "审批原因：缺少回滚方案" in updated.markdown


def test_resume_approval_requires_explicit_id(monkeypatch, tmp_path) -> None:
    aiops_api = importlib.import_module("app.api.aiops")
    service = ApprovalService(tmp_path / "approvals.db")
    old_request = service.create_request(
        ApprovalRequest(incident_id="inc-resume", action="旧变更", risk_level="medium")
    )
    approved = service.decide_request(
        old_request.approval_id,
        decision="approve",
        decided_by="sre",
    )
    monkeypatch.setattr(aiops_api, "get_approval_service", lambda: service)

    explicit_resume = aiops_api._resolve_resume_approval("inc-resume", approved.approval_id)
    assert explicit_resume.approval_id == approved.approval_id
