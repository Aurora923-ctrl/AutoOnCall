"""Tests for incident overview APIs used by the AIOps demo loop."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.aiops_read_models.replay_flow import build_replay_change_flow
from app.services.approval_service import ApprovalService
from app.services.report_generator import ReportGenerator
from app.services.sqlite_store import AIOpsSQLiteStore
from app.services.trace_service import TraceService


class FakeChangeExecutionService:
    """Small incident-scoped change execution service for API tests."""

    def __init__(self, executions: list[ChangeExecution] | None = None) -> None:
        self.executions = executions or []

    def list_executions(
        self,
        *,
        incident_id: str | None = None,
        change_plan_id: str | None = None,
    ) -> list[ChangeExecution]:
        return [
            execution
            for execution in self.executions
            if (incident_id is None or execution.incident_id == incident_id)
            and (change_plan_id is None or execution.change_plan_id == change_plan_id)
        ]


def test_incident_state_store_is_reused(monkeypatch, tmp_path) -> None:
    incidents_api = importlib.import_module("app.api.incidents")
    stores: list[AIOpsSQLiteStore] = []

    def create_store() -> AIOpsSQLiteStore:
        store = AIOpsSQLiteStore(tmp_path / f"states-{len(stores)}.db")
        stores.append(store)
        return store

    monkeypatch.setattr(incidents_api, "_incident_state_store", None)
    monkeypatch.setattr(incidents_api, "create_aiops_store", create_store)

    first = incidents_api.get_incident_state_store()
    second = incidents_api.get_incident_state_store()

    assert first is second
    assert stores == [first]


@pytest.mark.asyncio
async def test_incident_overview_aggregates_report_trace_and_approvals(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-demo"
    trace_id = "trace-demo"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    report = DiagnosisReport(
        incident_id=incident_id,
        trace_id=trace_id,
        title="order-service AIOps 诊断报告",
        service_name="order-service",
        severity="P1",
        environment="prod",
        status="waiting_approval",
        summary="Redis 连接数接近上限",
        root_cause="Redis maxclients 接近上限",
        evidence=[
            {
                "step_id": "s1",
                "source_tool": "query_redis_status",
                "data_source": "mock",
                "evidence_type": "redis",
                "stance": "supporting",
                "summary": "connected_clients=9940/10000",
                "fact": "Redis connected_clients=9940/10000；来源=mock",
                "inference": "该证据支持当前根因假设。",
                "uncertainty": "该证据来自 Mock 回退。",
                "next_step": "接入真实适配器后重复该步骤。",
                "confidence": 0.75,
            },
            {
                "step_id": "s2",
                "source_tool": "query_logs",
                "data_source": "loki",
                "evidence_type": "log",
                "stance": "supporting",
                "summary": "ERROR logs returned 2 entries",
                "confidence_reason": "日志后端返回错误信号",
                "confidence": 0.82,
            },
        ],
        tool_calls=[
            {
                "step_id": "s1",
                "tool_name": "query_redis_status",
                "data_source": "mock",
                "status": "success",
                "latency_ms": 12.5,
                "input_summary": '{"service_name": "order-service"}',
                "output_summary": "connected_clients=9940/10000",
            },
            {
                "step_id": "s2",
                "tool_name": "query_logs",
                "data_source": "loki",
                "status": "success",
                "latency_ms": 18.5,
                "input_summary": '{"service_name": "order-service"}',
                "output_summary": "ERROR logs returned 2 entries",
            },
        ],
        confirmed_facts=["Redis connected_clients=9940/10000；来源=mock"],
        inferred_conclusions=["该证据支持当前根因假设。"],
        uncertainties=["该证据来自 Mock 回退。"],
        next_steps=["接入真实适配器后重复该步骤。"],
        manual_action_required=True,
        approval_status="pending",
        trace_summary={"event_count": 1},
        markdown="# order-service AIOps 诊断报告",
        confidence=0.82,
    )
    reports.save_report(report)
    traces.create_event(
        trace_id=trace_id,
        incident_id=incident_id,
        node_name="executor",
        event_type="tool_call",
        status="success",
        output_summary="Redis 连接数接近上限",
    )
    approvals.create_request(
        ApprovalRequest(
            incident_id=incident_id,
            action="调整 Redis maxclients 配置",
            risk_level="high",
            reason="生产配置变更需要审批",
            metadata={"trace_id": trace_id},
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)

    overview = await incidents_api.get_incident_overview(incident_id)
    listing = await incidents_api.list_incidents()

    assert overview["incident_id"] == incident_id
    assert overview["trace_id"] == trace_id
    assert overview["status"] == "waiting_approval"
    assert overview["status_metadata"]["phase"] == "approval"
    assert overview["status_metadata"]["tone"] == "warning"
    assert overview["lifecycle"] is None
    assert overview["trace_summary"]["event_count"] == 1
    assert overview["approval_summary"]["by_status"]["pending"] == 1
    assert overview["diagnosis_chain"]["tool_calls"][0]["data_source"] == "mock"
    assert overview["diagnosis_chain"]["dependency_signals"] == []
    assert overview["diagnosis_chain"]["data_sources"]["has_mock"] is True
    assert overview["diagnosis_chain"]["confirmed_facts"]
    assert overview["diagnosis_chain"]["next_steps"]
    assert overview["links"]["report"] == f"/api/incidents/{incident_id}/report"
    assert listing["items"][0]["incident_id"] == incident_id

    approvals.decide_latest_pending(
        incident_id=incident_id,
        decision="approve",
        decided_by="pytest",
        reason="verified manual mitigation",
    )
    approved_overview = await incidents_api.get_incident_overview(incident_id)

    assert approved_overview["status"] == "approval_approved"
    assert approved_overview["approval_status"] == "approved"
    assert approved_overview["manual_action_required"] is True
    assert approved_overview["approval_summary"]["by_status"]["approved"] == 1


@pytest.mark.asyncio
async def test_incident_replay_aggregates_diagnosis_artifacts(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-replay"
    trace_id = "trace-replay"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            title="checkout-service AIOps 诊断报告",
            service_name="checkout-service",
            severity="P1",
            environment="prod",
            status="waiting_approval",
            summary="Redis 连接耗尽导致 checkout 超时",
            root_cause="Redis maxclients 接近上限",
            evidence=[
                {
                    "step_id": "s1",
                    "source_tool": "query_redis_status",
                    "data_source": "mock",
                    "evidence_type": "redis",
                    "stance": "supporting",
                    "summary": "connected_clients=9940/10000",
                    "confidence": 0.75,
                },
                {
                    "step_id": "s2",
                    "source_tool": "query_metrics",
                    "data_source": "prometheus",
                    "evidence_type": "metric",
                    "stance": "supporting",
                    "summary": "checkout p95 latency rose above threshold",
                    "confidence": 0.88,
                },
            ],
            tool_calls=[
                {
                    "step_id": "s1",
                    "tool_name": "query_redis_status",
                    "data_source": "mock",
                    "status": "success",
                    "latency_ms": 12.5,
                    "output_summary": "connected_clients=9940/10000",
                },
                {
                    "step_id": "s2",
                    "tool_name": "query_metrics",
                    "data_source": "prometheus",
                    "status": "success",
                    "latency_ms": 18.0,
                    "output_summary": "checkout p95 latency rose above threshold",
                },
            ],
            key_findings=["Redis connected_clients=9940/10000"],
            next_steps=["审批后执行受控 Redis 连接池恢复计划。"],
            confidence=0.82,
            markdown="# checkout-service AIOps 诊断报告",
        )
    )
    traces.create_event(
        trace_id=trace_id,
        incident_id=incident_id,
        node_name="planner",
        event_type="node",
        output_summary="plan_steps=2",
        metadata={
            "current_plan": [
                {
                    "step_id": "s1",
                    "tool_name": "query_redis_status",
                    "purpose": "确认 Redis 连接数",
                }
            ]
        },
    )
    traces.create_event(
        trace_id=trace_id,
        incident_id=incident_id,
        node_name="executor",
        event_type="tool_call",
        step_id="s1",
        tool_name="query_redis_status",
        status="success",
        output_summary="connected_clients=9940/10000",
        metadata={"data_source": "mock"},
        latency_ms=12.5,
    )
    traces.create_event(
        trace_id=trace_id,
        incident_id=incident_id,
        node_name="replanner",
        event_type="replan_decision",
        status="success",
        output_summary="证据充分，进入审批等待",
        metadata={
            "decision": "request_approval",
            "reason": "恢复动作需要审批",
            "decision_source": "llm_structured",
            "analysis_decision": "add_steps",
            "evidence_sufficient": True,
            "missing_evidence": ["query_logs"],
            "new_steps": [
                {
                    "step_id": "s3",
                    "tool_name": "query_logs",
                    "purpose": "补充 checkout 错误日志",
                    "expected_evidence": "Redis timeout 日志",
                    "risk_level": "low",
                }
            ],
            "conflicts": ["指标支持 Redis 连接耗尽，但日志尚未补齐"],
            "confidence_reasons": ["Redis 指标与延迟指标同时异常"],
            "evidence_profile": {
                "average_evidence_confidence": 0.82,
                "source_quality": "mixed_with_fallback",
                "by_data_source": {"mock": 1, "prometheus": 1},
            },
        },
    )
    approval = approvals.create_request(
        ApprovalRequest(
            incident_id=incident_id,
            action="调整 Redis maxclients 配置",
            risk_level="high",
            reason="生产配置变更需要审批",
            metadata={"trace_id": trace_id},
        )
    )
    change_service = FakeChangeExecutionService(
        [
            ChangeExecution(
                change_plan_id="chgplan-replay",
                approval_id=approval.approval_id,
                incident_id=incident_id,
                trace_id=trace_id,
                status="dry_run_completed",
            )
        ]
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)
    monkeypatch.setattr(incidents_api, "get_change_execution_service", lambda: change_service)
    monkeypatch.setattr(
        incidents_api,
        "get_eval_summary_for_replay",
        lambda: {
            "available": True,
            "cases": [
                {
                    "id": "replay-case",
                    "incident_id": incident_id,
                    "passed": True,
                    "metrics": {
                        "root_cause_hit": True,
                        "tool_hit": True,
                        "executed_tool_hit": True,
                        "evidence_count_hit": True,
                        "confidence_hit": True,
                        "report_contains_evidence": True,
                        "unnecessary_tool_rate": True,
                        "trace_completeness": True,
                    },
                    "planned_tools": ["query_redis_status", "query_metrics"],
                    "executed_tools": ["query_redis_status", "query_metrics"],
                    "failed_tools": [],
                    "failed_metrics": [],
                    "failure_reasons": {},
                    "unnecessary_tool_rate": 0.0,
                    "latency_ms": 123.45,
                }
            ],
        },
    )

    replay = await incidents_api.get_incident_replay(incident_id)
    stage_by_key = {stage["key"]: stage for stage in replay["stages"]}
    evaluation_metric_by_key = {metric["key"]: metric for metric in replay["evaluation"]["metrics"]}

    assert replay["incident_id"] == incident_id
    assert replay["links"]["replay"] == f"/api/incidents/{incident_id}/replay"
    assert replay["metrics"]["trace_event_count"] == 3
    assert replay["metrics"]["plan_step_count"] == 1
    assert replay["metrics"]["tool_call_count"] == 1
    assert replay["metrics"]["tool_call_record_count"] == 2
    assert replay["metrics"]["evidence_count"] == 2
    assert replay["metrics"]["approval_count"] == 1
    assert replay["metrics"]["change_execution_count"] == 1
    assert replay["metrics"]["replanner_decision_count"] == 1
    assert replay["replanner_decisions"][0]["decision"] == "request_approval"
    assert replay["replanner_decisions"][0]["decision_label"] == "请求审批"
    assert replay["replanner_decisions"][0]["decision_source"] == "llm_structured"
    assert replay["replanner_decisions"][0]["decision_source_label"] == "LLM 结构化决策"
    assert replay["replanner_decisions"][0]["analysis_decision"] == "add_steps"
    assert replay["replanner_decisions"][0]["analysis_decision_label"] == "追加证据"
    assert replay["replanner_decisions"][0]["evidence_sufficient"] is True
    assert replay["replanner_decisions"][0]["missing_evidence"] == ["query_logs"]
    assert replay["replanner_decisions"][0]["new_steps"][0]["tool_name"] == "query_logs"
    assert replay["replanner_decisions"][0]["source_quality"] == "mixed_with_fallback"
    assert replay["evidence_quality"]["has_mock"] is True
    assert replay["evidence_quality"]["by_source"]["prometheus"] == 1
    assert replay["tooling"]["by_tool"]["query_redis_status"] == 1
    assert replay["tooling"]["trace_report_count_mismatch"] is True
    assert replay["approval_flow"]["summary"]["status"] == "pending"
    assert replay["approval_flow"]["before_after"]["approved_to_continue"] is False
    assert replay["change_flow"]["status"] == "change_validated"
    assert replay["change_flow"]["latest"]["status"] == "dry_run_completed"
    assert replay["report_summary"]["root_cause"] == "Redis maxclients 接近上限"
    assert replay["evaluation"]["status"] == "passed"
    assert replay["evaluation"]["linked"] is True
    assert replay["evaluation"]["case_id"] == "replay-case"
    assert evaluation_metric_by_key["root_cause_hit"]["status"] == "passed"
    assert evaluation_metric_by_key["tool_hit"]["status"] == "passed"
    assert evaluation_metric_by_key["evidence_sufficient"]["value"] is True
    assert evaluation_metric_by_key["tool_redundancy"]["value"] == 0.0
    assert evaluation_metric_by_key["latency_ms"]["value"] == 123.45
    assert stage_by_key["planner"]["status"] == "completed"
    assert stage_by_key["executor"]["event_count"] == 1
    assert stage_by_key["replanner"]["status"] == "completed"
    assert stage_by_key["evaluation"]["status"] == "passed"
    assert any(item["stage"] == "executor" for item in replay["timeline"])


@pytest.mark.asyncio
async def test_incident_replay_isolates_latest_report_trace_and_approval(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-replay-isolation"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id="trace-new",
            title="Latest report",
            status="completed",
            markdown="# Latest report",
        )
    )
    traces.create_event(
        trace_id="trace-old",
        incident_id=incident_id,
        node_name="executor",
        event_type="tool_call",
        tool_name="old_tool",
    )
    traces.create_event(
        trace_id="trace-new",
        incident_id=incident_id,
        node_name="executor",
        event_type="tool_call",
        tool_name="new_tool",
    )
    approvals.create_request(
        ApprovalRequest(
            incident_id=incident_id,
            action="old approval",
            risk_level="high",
            metadata={"trace_id": "trace-old", "session_id": "session-old"},
        )
    )
    new_approval = approvals.create_request(
        ApprovalRequest(
            incident_id=incident_id,
            action="new approval",
            risk_level="high",
            metadata={"trace_id": "trace-new", "session_id": "session-new"},
        )
    )
    states.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            trace_id="trace-new",
            session_id="session-new",
            report_id=reports.get_report(incident_id).report_id,
            status="waiting_approval",
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)
    monkeypatch.setattr(
        incidents_api,
        "get_change_execution_service",
        lambda: FakeChangeExecutionService(),
    )
    monkeypatch.setattr(incidents_api, "get_eval_summary_for_replay", lambda: None)

    replay = await incidents_api.get_incident_replay(incident_id)

    assert replay["trace_id"] == "trace-new"
    assert {item["trace_id"] for item in replay["timeline"]} == {"trace-new"}
    assert replay["approval_flow"]["summary"]["total"] == 1
    assert replay["approval_flow"]["items"][0]["approval_id"] == new_approval.approval_id
    assert "old_tool" not in replay["tooling"]["by_tool"]


@pytest.mark.asyncio
async def test_incident_replay_isolates_change_executions_by_trace(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-replay-change-isolation"
    trace_id = "trace-current"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    report = reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            title="Current report",
            markdown="# Current report",
        )
    )
    states.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            trace_id=trace_id,
            report_id=report.report_id,
            status="completed",
        )
    )
    change_service = FakeChangeExecutionService(
        [
            ChangeExecution(
                change_plan_id="plan-old",
                approval_id="approval-old",
                incident_id=incident_id,
                trace_id="trace-old",
                status="dry_run_completed",
            ),
            ChangeExecution(
                change_plan_id="plan-current",
                approval_id="approval-current",
                incident_id=incident_id,
                trace_id=trace_id,
                status="dry_run_completed",
            ),
        ]
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)
    monkeypatch.setattr(incidents_api, "get_change_execution_service", lambda: change_service)
    monkeypatch.setattr(incidents_api, "get_eval_summary_for_replay", lambda: None)

    replay = await incidents_api.get_incident_replay(incident_id)

    assert replay["change_flow"]["total"] == 1
    assert replay["change_flow"]["items"][0]["trace_id"] == trace_id


@pytest.mark.asyncio
async def test_incident_overview_counts_only_selected_trace_artifacts(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-overview-artifact-isolation"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    report = reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id="trace-current",
            title="Current report",
            markdown="# Current report",
        )
    )
    for trace_id in ("trace-old", "trace-current"):
        traces.create_event(
            trace_id=trace_id,
            incident_id=incident_id,
            node_name="executor",
            event_type="tool_call",
            tool_name=f"tool-{trace_id}",
        )
        approvals.create_request(
            ApprovalRequest(
                incident_id=incident_id,
                action=f"approval-{trace_id}",
                risk_level="high",
                metadata={"trace_id": trace_id},
            )
        )
    states.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            trace_id="trace-current",
            report_id=report.report_id,
            status="waiting_approval",
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)

    overview = await incidents_api.get_incident_overview(incident_id)

    assert overview["trace_summary"]["event_count"] == 1
    assert overview["approval_summary"]["total"] == 1
    assert overview["approval_status"] == "pending"


@pytest.mark.asyncio
async def test_incident_trace_prefers_newer_state_even_with_one_old_trace(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-trace-new-state"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    old_report = reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id="trace-old",
            title="Old report",
            markdown="# Old report",
        )
    )
    traces.create_event(
        trace_id="trace-old",
        incident_id=incident_id,
        node_name="executor",
        output_summary="old run event",
    )
    states.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            trace_id="trace-new",
            session_id="session-new",
            status="investigating",
            updated_at=old_report.created_at + timedelta(seconds=1),
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)

    payload = await incidents_api.get_incident_trace(incident_id)

    assert payload["trace_id"] == "trace-new"
    assert payload["items"] == []


@pytest.mark.asyncio
async def test_incident_trace_api_redacts_legacy_sensitive_payloads(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-trace-redaction"
    trace_id = "trace-redaction"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            title="Trace redaction",
            markdown="# Trace redaction",
        )
    )
    traces._store.save_trace_event(
        TraceEvent(
            trace_id=trace_id,
            incident_id=incident_id,
            node_name="executor",
            event_type="tool_call",
            tool_args={"authorization": "Bearer legacy-secret"},
            tool_result={"password": "legacy-password"},
            output_summary="token=legacy-token",
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(
        incidents_api,
        "get_approval_service",
        lambda: ApprovalService(tmp_path / "approvals.db"),
    )
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)
    monkeypatch.setattr(
        incidents_api,
        "get_change_execution_service",
        lambda: FakeChangeExecutionService(),
    )

    trace = await incidents_api.get_incident_trace(incident_id)
    serialized = str(trace)

    assert "legacy-secret" not in serialized
    assert "legacy-password" not in serialized
    assert "legacy-token" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.asyncio
async def test_incident_report_api_redacts_legacy_sensitive_payloads(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-report-redaction"
    reports = ReportGenerator(tmp_path / "reports.db")
    reports._store.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id="trace-report-redaction",
            summary="token=legacy-report-secret",
            evidence=[{"raw_data": {"password": "legacy-evidence-secret"}}],
            markdown="# Report\n\ncookie=legacy-markdown-secret",
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)

    payload = await incidents_api.get_incident_report(incident_id)
    serialized = str(payload)

    assert "legacy-report-secret" not in serialized
    assert "legacy-evidence-secret" not in serialized
    assert "legacy-markdown-secret" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.asyncio
async def test_incident_overview_prefers_durable_lifecycle_state(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-lifecycle"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id="trace-lifecycle",
            title="order-service AIOps 诊断报告",
            service_name="order-service",
            severity="P1",
            environment="prod",
            status="approval_approved",
            summary="审批已通过",
            markdown="# report",
        )
    )
    states.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            status="change_dry_run",
            status_reason="Safe change workflow status=dry_run_running",
            title="order-service AIOps 诊断报告",
            service_name="order-service",
            severity="P1",
            environment="prod",
            trace_id="trace-lifecycle",
            session_id="session-lifecycle",
            approval_status="approved",
            manual_action_required=True,
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)

    overview = await incidents_api.get_incident_overview(incident_id)

    assert overview["status"] == "change_dry_run"
    assert overview["status_metadata"]["phase"] == "change"
    assert overview["status_reason"] == "Safe change workflow status=dry_run_running"
    assert overview["session_id"] == "session-lifecycle"
    assert overview["lifecycle"]["status"] == "change_dry_run"


@pytest.mark.asyncio
async def test_incident_overview_keeps_report_conclusions_authoritative_for_same_run(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-report-conclusion-authority"
    trace_id = "trace-report-conclusion-authority"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    report = reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            summary="report summary",
            root_cause="report root cause",
            markdown="# report",
        )
    )
    states.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            trace_id=trace_id,
            report_id=report.report_id,
            status="completed",
            summary="stale lifecycle summary",
            root_cause="stale lifecycle root cause",
            updated_at=report.created_at + timedelta(seconds=1),
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)

    overview = await incidents_api.get_incident_overview(incident_id)

    assert overview["summary"] == "report summary"
    assert overview["root_cause"] == "report root cause"
    assert overview["report"]["summary"] == overview["summary"]
    assert overview["report"]["root_cause"] == overview["root_cause"]
    assert overview["lifecycle"]["summary"] == "stale lifecycle summary"


def test_replay_change_flow_sorts_by_update_time_before_selecting_latest() -> None:
    replay = build_replay_change_flow(
        [
            {
                "change_execution_id": "change-new",
                "trace_id": "trace-change-order",
                "status": "closed",
                "created_at": "2026-07-18T01:00:00+00:00",
                "updated_at": "2026-07-18T03:00:00+00:00",
            },
            {
                "change_execution_id": "change-old",
                "trace_id": "trace-change-order",
                "status": "waiting_manual_execution",
                "created_at": "2026-07-18T02:00:00+00:00",
                "updated_at": "2026-07-18T02:00:00+00:00",
            },
        ]
    )

    assert [item["change_execution_id"] for item in replay["items"]] == [
        "change-old",
        "change-new",
    ]
    assert replay["latest"]["change_execution_id"] == "change-new"
    assert replay["status"] == "closed"


def test_replay_change_flow_has_deterministic_tie_breaker() -> None:
    timestamp = "2026-07-18T03:00:00+00:00"

    replay = build_replay_change_flow(
        [
            {
                "change_execution_id": "change-b",
                "status": "closed",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
            {
                "change_execution_id": "change-a",
                "status": "dry_run_completed",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        ]
    )

    assert [item["change_execution_id"] for item in replay["items"]] == [
        "change-a",
        "change-b",
    ]
    assert replay["latest"]["change_execution_id"] == "change-b"


def test_sqlite_latest_report_list_matches_single_incident_lookup_for_out_of_order_writes(
    tmp_path,
) -> None:
    store = AIOpsSQLiteStore(tmp_path / "report-order.db")
    created_at = datetime(2026, 7, 18, tzinfo=UTC)
    newer = DiagnosisReport(
        report_id="report-newer",
        incident_id="inc-report-order",
        trace_id="trace-newer",
        created_at=created_at + timedelta(seconds=10),
    )
    older = DiagnosisReport(
        report_id="report-older",
        incident_id="inc-report-order",
        trace_id="trace-older",
        created_at=created_at,
    )
    store.save_report(newer)
    store.save_report(older)

    listed = {report.incident_id: report for report in store.list_latest_reports()}

    assert store.get_latest_report(newer.incident_id).report_id == newer.report_id
    assert listed[newer.incident_id].report_id == newer.report_id


@pytest.mark.asyncio
async def test_incident_overview_uses_newer_state_without_mixing_older_report(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-new-run-without-report"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    old_report = reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id="trace-old",
            title="Old report",
            summary="old summary",
            root_cause="old root cause",
            markdown="# Old report",
        )
    )
    states.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            trace_id="trace-new",
            session_id="session-new",
            status="investigating",
            title="New investigation",
            summary="new summary",
            updated_at=old_report.created_at + timedelta(seconds=1),
        )
    )
    traces.create_event(
        trace_id="trace-new",
        incident_id=incident_id,
        node_name="planner",
        output_summary="new run started",
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)
    monkeypatch.setattr(
        incidents_api,
        "get_change_execution_service",
        lambda: FakeChangeExecutionService(),
    )
    monkeypatch.setattr(incidents_api, "get_eval_summary_for_replay", lambda: None)

    overview = await incidents_api.get_incident_overview(incident_id)
    replay = await incidents_api.get_incident_replay(incident_id)

    assert overview["trace_id"] == "trace-new"
    assert overview["title"] == "New investigation"
    assert overview["summary"] == "new summary"
    assert overview["root_cause"] == ""
    assert overview["report"] is None
    assert overview["diagnosis_chain"]["evidence"] == []
    assert replay["trace_id"] == "trace-new"
    assert replay["report_summary"]["available"] is False
    assert replay["root_cause"] == ""


@pytest.mark.asyncio
async def test_incident_replay_counts_only_actual_tool_invocations(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-replay-tool-kinds"
    trace_id = "trace-replay-tool-kinds"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            tool_calls=[
                {
                    "tool_name": "query_metrics",
                    "status": "success",
                    "actual_tool_invoked": True,
                    "invocation_kind": "tool",
                },
                {
                    "tool_name": "manual_analysis",
                    "status": "success",
                    "actual_tool_invoked": False,
                    "invocation_kind": "analysis_fallback",
                },
            ],
            markdown="# Replay tool kinds",
        )
    )
    traces.create_event(
        trace_id=trace_id,
        incident_id=incident_id,
        node_name="executor",
        event_type="tool_call",
        tool_name="query_metrics",
        metadata={
            "actual_tool_invoked": True,
            "invocation_kind": "tool",
        },
    )
    traces.create_event(
        trace_id=trace_id,
        incident_id=incident_id,
        node_name="executor",
        event_type="tool_call",
        tool_name="manual_analysis",
        metadata={
            "actual_tool_invoked": False,
            "invocation_kind": "analysis_fallback",
        },
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)
    monkeypatch.setattr(
        incidents_api,
        "get_change_execution_service",
        lambda: FakeChangeExecutionService(),
    )
    monkeypatch.setattr(incidents_api, "get_eval_summary_for_replay", lambda: None)

    replay = await incidents_api.get_incident_replay(incident_id)

    assert replay["metrics"]["tool_call_count"] == 1
    assert replay["metrics"]["tool_call_record_count"] == 2
    assert replay["tooling"]["total"] == 1
    assert replay["tooling"]["audit_record_total"] == 2
    assert replay["tooling"]["non_tool_record_count"] == 1
    assert replay["tooling"]["by_tool"] == {"query_metrics": 1}
    assert replay["tooling"]["by_invocation_kind"]["analysis_fallback"] == 1


@pytest.mark.asyncio
async def test_incident_replay_quality_ignores_unusable_evidence_confidence(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-replay-unusable-evidence"
    trace_id = "trace-replay-unusable-evidence"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            confidence=0.9,
            evidence=[
                {
                    "evidence_id": "evd-failed",
                    "evidence_type": "metrics",
                    "data_source": "prometheus",
                    "confidence": 0.99,
                    "raw_data": {"status": "failed"},
                },
                {
                    "evidence_id": "evd-stale",
                    "evidence_type": "logs",
                    "data_source": "loki",
                    "confidence": 0.98,
                    "raw_data": {
                        "status": "success",
                        "metadata": {"evidence_quality": {"usable": False}},
                    },
                },
            ],
            markdown="# Replay unusable evidence",
        )
    )
    traces.create_event(
        trace_id=trace_id,
        incident_id=incident_id,
        node_name="executor",
        event_type="tool_call",
        tool_name="query_metrics",
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)
    monkeypatch.setattr(
        incidents_api,
        "get_change_execution_service",
        lambda: FakeChangeExecutionService(),
    )
    monkeypatch.setattr(incidents_api, "get_eval_summary_for_replay", lambda: None)

    replay = await incidents_api.get_incident_replay(incident_id)
    metric_by_key = {item["key"]: item for item in replay["evaluation"]["metrics"]}

    assert replay["evidence_quality"]["usable_count"] == 0
    assert replay["evidence_quality"]["average_confidence"] == 0.0
    assert replay["evidence_quality"]["all_evidence_average_confidence"] == 0.985
    assert metric_by_key["evidence_sufficient"]["value"] is False


@pytest.mark.asyncio
async def test_incident_replay_does_not_link_stale_evaluation_summary(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-stale-eval"
    trace_id = "trace-stale-eval"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            title="Redis saturation",
            markdown="# stale eval",
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)
    monkeypatch.setattr(
        incidents_api,
        "get_change_execution_service",
        lambda: FakeChangeExecutionService(),
    )
    monkeypatch.setattr(
        incidents_api,
        "get_eval_summary_for_replay",
        lambda: {
            "available": False,
            "stale": True,
            "artifact": "eval-summary.json",
            "artifact_status": {
                "stale": True,
                "reasons": ["git_commit_changed"],
                "generated_fingerprint": "old",
                "current_fingerprint": "new",
            },
            "run": {
                "run_id": "run-old",
                "environment": {
                    "git_commit": "old-commit",
                    "git_dirty": False,
                    "evaluation_fingerprint": "old",
                },
            },
            "cases": [{"id": incident_id, "passed": True, "metrics": {}}],
        },
    )

    replay = await incidents_api.get_incident_replay(incident_id)

    assert replay["evaluation"]["linked"] is False
    assert replay["evaluation"]["passed"] is None
    assert replay["evaluation"]["provenance"]["stale"] is True
    assert replay["evaluation"]["provenance"]["stale_reasons"] == ["git_commit_changed"]


@pytest.mark.asyncio
async def test_incident_replay_exposes_eval_match_and_provenance(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-current-eval"
    trace_id = "trace-current-eval"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            title="Redis saturation",
            markdown="# current eval",
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)
    monkeypatch.setattr(
        incidents_api,
        "get_change_execution_service",
        lambda: FakeChangeExecutionService(),
    )
    monkeypatch.setattr(
        incidents_api,
        "get_eval_summary_for_replay",
        lambda: {
            "available": True,
            "stale": False,
            "artifact": "eval-summary.json",
            "artifact_status": {"stale": False, "reasons": []},
            "run": {
                "run_id": "run-current",
                "dataset": {"sha256": "dataset-sha"},
                "environment": {
                    "git_commit": "current-commit",
                    "git_dirty": False,
                    "evaluation_fingerprint": "fingerprint",
                },
            },
            "cases": [
                {
                    "id": incident_id,
                    "passed": True,
                    "metrics": {},
                    "unnecessary_tool_rate": 0.25,
                }
            ],
        },
    )

    replay = await incidents_api.get_incident_replay(incident_id)
    metric_by_key = {item["key"]: item for item in replay["evaluation"]["metrics"]}

    assert replay["evaluation"]["linked"] is True
    assert replay["evaluation"]["match"] == {"method": "identifier", "score": 1}
    assert replay["evaluation"]["provenance"]["run_id"] == "run-current"
    assert replay["evaluation"]["provenance"]["git_commit"] == "current-commit"
    assert replay["evaluation"]["provenance"]["dataset"]["sha256"] == "dataset-sha"
    assert metric_by_key["tool_redundancy"]["status"] == "warning"


@pytest.mark.asyncio
async def test_incident_replay_does_not_treat_mock_only_evidence_as_sufficient(
    monkeypatch,
    tmp_path,
) -> None:
    incident_id = "inc-mock-only-replay"
    trace_id = "trace-mock-only-replay"
    reports = ReportGenerator(tmp_path / "reports.db")
    traces = TraceService(tmp_path / "traces.db")
    approvals = ApprovalService(tmp_path / "approvals.db")
    states = AIOpsSQLiteStore(tmp_path / "states.db")
    reports.save_report(
        DiagnosisReport(
            incident_id=incident_id,
            trace_id=trace_id,
            confidence=0.9,
            evidence=[
                {
                    "evidence_id": "evd-mock-1",
                    "evidence_type": "metric",
                    "data_source": "mock",
                    "confidence": 0.9,
                    "raw_data": {"status": "success"},
                },
                {
                    "evidence_id": "evd-mock-2",
                    "evidence_type": "log",
                    "data_source": "mock",
                    "confidence": 0.9,
                    "raw_data": {"status": "success"},
                },
            ],
            markdown="# mock only",
        )
    )

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: reports)
    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: traces)
    monkeypatch.setattr(incidents_api, "get_approval_service", lambda: approvals)
    monkeypatch.setattr(incidents_api, "get_incident_state_store", lambda: states)
    monkeypatch.setattr(
        incidents_api,
        "get_change_execution_service",
        lambda: FakeChangeExecutionService(),
    )
    monkeypatch.setattr(incidents_api, "get_eval_summary_for_replay", lambda: None)

    replay = await incidents_api.get_incident_replay(incident_id)
    metric_by_key = {item["key"]: item for item in replay["evaluation"]["metrics"]}

    assert replay["evidence_quality"]["usable_count"] == 2
    assert replay["evidence_quality"]["trusted_usable_count"] == 0
    assert metric_by_key["evidence_sufficient"]["value"] is False


@pytest.mark.asyncio
async def test_incident_overview_returns_404_for_unknown_incident(monkeypatch, tmp_path) -> None:
    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(
        incidents_api,
        "get_report_generator",
        lambda: ReportGenerator(tmp_path / "reports.db"),
    )
    monkeypatch.setattr(
        incidents_api,
        "get_trace_service",
        lambda: TraceService(tmp_path / "traces.db"),
    )
    monkeypatch.setattr(
        incidents_api,
        "get_approval_service",
        lambda: ApprovalService(tmp_path / "approvals.db"),
    )
    monkeypatch.setattr(
        incidents_api,
        "get_incident_state_store",
        lambda: AIOpsSQLiteStore(tmp_path / "states.db"),
    )
    monkeypatch.setattr(
        incidents_api,
        "get_change_execution_service",
        lambda: FakeChangeExecutionService(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await incidents_api.get_incident_overview("inc-missing")

    assert exc_info.value.status_code == 404

    with pytest.raises(HTTPException) as replay_exc_info:
        await incidents_api.get_incident_replay("inc-missing")

    assert replay_exc_info.value.status_code == 404

    with pytest.raises(HTTPException) as trace_exc_info:
        await incidents_api.get_incident_trace("inc-missing")

    assert trace_exc_info.value.status_code == 404
