"""Tests for the local human approval service."""

import importlib
import json
import sqlite3

import pytest

from app.models.approval import ApprovalRequest
from app.models.report import DiagnosisReport
from app.services.approval_service import ApprovalService, ApprovalStateError
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
