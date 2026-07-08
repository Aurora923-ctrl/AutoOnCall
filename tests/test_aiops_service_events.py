"""Tests for AIOps service SSE event formatting."""

import importlib

import pytest

from app.services.aiops_progress import build_progress_payload
from app.services.aiops_service import (
    _build_fallback_final_response,
    _merge_checkpoint_with_node_output,
    _terminal_event_status,
    aiops_service,
)
from app.services.report_generator import ReportGenerator
from app.utils.public_errors import GENERIC_DIAGNOSIS_ERROR

REQUIRED_PROGRESS_FIELDS = {
    "phase",
    "node_name",
    "current_tool",
    "tool_total",
    "tool_success_count",
    "tool_failed_count",
    "evidence_count",
    "risk_policy",
    "report_status",
    "cursor",
}


def test_planner_event_includes_structured_plan_for_new_clients() -> None:
    current_plan = [
        {
            "step_id": "s1",
            "tool_name": "query_metrics",
            "purpose": "检查指标",
            "input_args": {"service_name": "order-service"},
            "expected_evidence": "指标异常证据",
            "risk_level": "low",
            "status": "pending",
        }
    ]

    event = aiops_service._format_planner_event(
        {
            "plan": ["[s1] 使用 query_metrics: 检查指标"],
            "current_plan": current_plan,
        }
    )

    assert event["type"] == "plan"
    assert event["plan"]
    assert event["current_plan"] == current_plan


def test_executor_event_exposes_evidence_tool_records_and_result_preview() -> None:
    event = aiops_service._format_executor_event(
        {
            "plan": [],
            "past_steps": [("检查 Redis", "connected_clients=9940/10000")],
            "gathered_evidence": [{"source_tool": "query_redis_status"}],
            "tool_call_records": [{"tool_name": "query_redis_status", "status": "success"}],
            "errors": [],
        }
    )

    assert event["type"] == "step_complete"
    assert event["result_preview"] == "connected_clients=9940/10000"
    assert event["evidence"][0]["source_tool"] == "query_redis_status"
    assert event["tool_call_records"][0]["tool_name"] == "query_redis_status"


def test_progress_payload_exposes_stable_recovery_contract() -> None:
    progress = build_progress_payload(
        {
            "session_id": "session-progress",
            "trace_id": "trace-progress",
            "incident": {"incident_id": "inc-progress"},
            "current_plan": [
                {"step_id": "s2", "tool_name": "query_logs", "status": "pending"}
            ],
            "past_steps": [("metrics", "ok")],
            "tool_call_records": [
                {"tool_name": "query_metrics", "status": "success"},
                {"tool_name": "query_redis_status", "status": "failed"},
            ],
            "gathered_evidence": [{"summary": "metrics ok"}],
            "risk_assessment": {"policy": "allow"},
        },
        phase="executing",
        node_name="executor",
        cursor="session-progress:000002",
    )

    assert REQUIRED_PROGRESS_FIELDS <= set(progress)
    assert progress["phase"] == "executing"
    assert progress["current_tool"] == "query_logs"
    assert progress["tool_total"] == 2
    assert progress["tool_success_count"] == 1
    assert progress["tool_failed_count"] == 1
    assert progress["evidence_count"] == 1
    assert progress["risk_policy"] == "allow"
    assert progress["report_status"] == "not_started"
    assert progress["cursor"] == "session-progress:000002"


def test_replanner_event_returns_approval_required_when_pending_approval_exists() -> None:
    event = aiops_service._format_replanner_event(
        {
            "response": "# AIOps 诊断已暂停，等待人工审批",
            "pending_approval": {
                "approval_id": "apr-1",
                "incident_id": "inc-1",
                "action": "重启生产服务",
                "risk_level": "high",
                "status": "pending",
            },
            "risk_assessment": {
                "risk_level": "high",
                "action": "重启生产服务",
                "need_approval": True,
            },
        }
    )

    assert event["type"] == "approval_required"
    assert event["pending_approval"]["approval_id"] == "apr-1"
    assert event["risk_assessment"]["need_approval"] is True


def test_replanner_report_event_keeps_markdown_and_adds_structured_report() -> None:
    structured_report = {
        "incident_id": "inc-1",
        "trace_id": "trace-1",
        "root_cause": "Redis 连接数接近上限",
        "markdown": "# order-service AIOps 诊断报告",
    }

    event = aiops_service._format_replanner_event(
        {
            "response": "# order-service AIOps 诊断报告",
            "report": structured_report,
            "hypotheses": ["Redis 连接数接近上限"],
            "final_diagnosis": "Redis 连接数接近上限",
        }
    )

    assert event["type"] == "report"
    assert event["report"] == "# order-service AIOps 诊断报告"
    assert event["structured_report"] == structured_report


def test_fallback_final_response_is_not_empty_when_graph_ends_without_response() -> None:
    response = _build_fallback_final_response(
        {
            "incident": {
                "incident_id": "inc-empty",
                "service_name": "order-service",
                "symptom": "Redis timeout",
            },
            "past_steps": [("检查指标", "P95 high")],
            "errors": ["replanner returned empty response"],
        }
    )

    assert response.startswith("# AIOps 诊断流程已结束")
    assert "inc-empty" in response
    assert "replanner returned empty response" in response


def test_snapshot_merge_keeps_cumulative_additive_fields_without_duplicates() -> None:
    merged = _merge_checkpoint_with_node_output(
        {
            "past_steps": [("s1", "ok"), ("s2", "ok")],
            "gathered_evidence": [{"step_id": "s1"}, {"step_id": "s2"}],
            "response": "",
        },
        {
            "past_steps": [("s2", "ok")],
            "gathered_evidence": [{"step_id": "s2"}],
            "response": "done",
        },
    )

    assert merged["past_steps"] == [("s1", "ok"), ("s2", "ok")]
    assert merged["gathered_evidence"] == [{"step_id": "s1"}, {"step_id": "s2"}]
    assert merged["response"] == "done"


def test_snapshot_merge_appends_additive_delta_when_checkpoint_is_stale() -> None:
    merged = _merge_checkpoint_with_node_output(
        {"past_steps": [("s1", "ok")]},
        {"past_steps": [("s2", "ok")]},
    )

    assert merged["past_steps"] == [("s1", "ok"), ("s2", "ok")]


def test_terminal_event_status_prefers_structured_report_status() -> None:
    assert (
        _terminal_event_status(
            {
                "type": "complete",
                "structured_report": {"status": "waiting_approval"},
                "diagnosis": {"status": "completed"},
            }
        )
        == "waiting_approval"
    )
    assert (
        _terminal_event_status(
            {
                "type": "complete",
                "risk_assessment": {"policy": "forbidden"},
            }
        )
        == "blocked"
    )
    assert _terminal_event_status({"type": "error"}) == "failed"


@pytest.mark.asyncio
async def test_execute_complete_generates_structured_report_when_graph_has_no_report(
    monkeypatch,
    tmp_path,
) -> None:
    service_module = importlib.import_module("app.services.aiops_service")
    monkeypatch.setattr(
        service_module, "report_generator", ReportGenerator(tmp_path / "reports.db")
    )

    async def fake_astream(input, config, stream_mode):
        yield {
            "planner": {
                "plan": [],
                "current_plan": [],
            }
        }

    class FakeState:
        values = {
            "input": "order-service timeout",
            "incident": {
                "incident_id": "inc-fallback",
                "service_name": "order-service",
                "severity": "P2",
                "symptom": "timeout",
                "environment": "prod",
            },
            "trace_id": "trace-fallback",
            "past_steps": [],
            "gathered_evidence": [],
            "tool_call_records": [],
            "errors": ["graph ended without response"],
        }

    class FakeGraph:
        def astream(self, input, config, stream_mode):
            return fake_astream(input, config, stream_mode)

        def get_state(self, config):
            return FakeState()

    service = service_module.AIOpsService()
    service.graph = FakeGraph()

    events = [event async for event in service.execute("order-service timeout", "fallback-session")]
    complete = events[-1]
    progress_events = [event for event in events if event["type"] == "progress"]

    assert progress_events
    assert all(REQUIRED_PROGRESS_FIELDS <= set(event["progress"]) for event in progress_events)
    assert progress_events[0]["progress"]["cursor"] == "fallback-session:000001"
    assert progress_events[-1]["progress"]["phase"] == "complete"
    assert complete["type"] == "complete"
    assert complete["status"] == "escalated"
    assert complete["structured_report"]["incident_id"] == "inc-fallback"
    assert complete["structured_report"]["status"] == "escalated"
    assert complete["response"] == complete["structured_report"]["markdown"]
    assert complete["progress"]["phase"] == "complete"
    assert complete["progress_cursor"] == complete["progress"]["cursor"]


@pytest.mark.asyncio
async def test_execute_error_event_uses_public_message_without_raw_exception() -> None:
    service_module = importlib.import_module("app.services.aiops_service")

    async def fake_astream(input, config, stream_mode):
        raise RuntimeError("mysql://user:secret@db.internal/orders unavailable")
        yield {}

    class FakeGraph:
        def astream(self, input, config, stream_mode):
            return fake_astream(input, config, stream_mode)

    service = service_module.AIOpsService()
    service.graph = FakeGraph()

    events = [event async for event in service.execute("orders incident", "error-session")]
    error_event = events[-1]
    progress_events = [event for event in events if event["type"] == "progress"]
    serialized = str(error_event)

    assert progress_events[-1]["progress"]["phase"] == "error"
    assert progress_events[-1]["progress"]["cursor"] == "error-session:000002"
    assert error_event["type"] == "error"
    assert error_event["progress"]["phase"] == "error"
    assert error_event["message"] == GENERIC_DIAGNOSIS_ERROR
    assert error_event["trace_event"]["error_message"] == GENERIC_DIAGNOSIS_ERROR
    assert "secret" not in serialized
    assert "db.internal" not in serialized
    assert "orders unavailable" not in serialized
