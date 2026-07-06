"""Tests for incident overview APIs used by the AIOps demo loop."""

import importlib

import pytest
from fastapi import HTTPException

from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
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
                "source_tool": "query_traces",
                "data_source": "jaeger",
                "evidence_type": "trace",
                "stance": "supporting",
                "summary": "Jaeger 返回 2 条 trace，error_spans=1",
                "confidence_reason": "Tracing 后端返回调用链耗时和错误 span 信号",
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
                "tool_name": "query_traces",
                "data_source": "jaeger",
                "status": "success",
                "latency_ms": 18.5,
                "input_summary": '{"service_name": "order-service"}',
                "output_summary": "Jaeger 返回 2 条 trace，error_spans=1",
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
    assert overview["diagnosis_chain"]["dependency_signals"][0]["backend"] == "jaeger"
    assert overview["diagnosis_chain"]["dependency_signals"][0]["domain"] == "tracing"
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
    assert replay["metrics"]["tool_call_count"] == 2
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
