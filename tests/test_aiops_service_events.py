"""Tests for AIOps service SSE event formatting."""

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

import app.api.aiops_route_helpers as route_helpers_module
import app.services.aiops_service as aiops_service_module
from app.api.aiops_route_helpers import (
    diagnosis_event_stream,
    resume_diagnosis_event_stream,
    safe_change_event_stream,
)
from app.services.aiops_progress import build_progress_payload
from app.services.aiops_service import (
    AIOpsService,
    _build_fallback_final_response,
    _merge_checkpoint_with_node_output,
    _terminal_event_status,
    aiops_service,
)
from app.services.report_generator import ReportGenerator
from app.services.sqlite_store import AIOpsSQLiteStore
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


def test_aiops_request_trace_metadata_uses_incident_evidence_level() -> None:
    incident = aiops_service_module.Incident(
        incident_id="inc-live-trace",
        title="Redis saturation",
        service_name="order-service",
        severity="P2",
        symptom="Redis timeout",
        environment="local-live",
        raw_alert={"evidence_level": "local_live"},
    )

    metadata = aiops_service_module._request_trace_metadata("session-live", incident)

    assert metadata == {
        "request_id": "session-live",
        "request_kind": "aiops",
        "evidence_level": "local_live",
        "path": "/api/aiops",
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


def test_executor_event_accepts_durable_normalized_past_step() -> None:
    event = aiops_service._format_executor_event(
        {
            "plan": None,
            "past_steps": [
                {
                    "step": {"step_id": "s1", "tool_name": "query_metrics"},
                    "result": {"status": "success", "summary": "P95 high"},
                }
            ],
            "gathered_evidence": None,
            "tool_call_records": None,
            "errors": None,
            "warnings": None,
        }
    )

    assert event["type"] == "step_complete"
    assert event["current_step"]["step_id"] == "s1"
    assert "P95 high" in event["result_preview"]
    assert event["remaining_steps"] == 0


def test_progress_payload_exposes_stable_recovery_contract() -> None:
    progress = build_progress_payload(
        {
            "session_id": "session-progress",
            "trace_id": "trace-progress",
            "incident": {"incident_id": "inc-progress"},
            "current_plan": [{"step_id": "s2", "tool_name": "query_logs", "status": "pending"}],
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


def test_snapshot_merge_keeps_run_identity_from_checkpoint() -> None:
    merged = _merge_checkpoint_with_node_output(
        {
            "session_id": "session-canonical",
            "trace_id": "trace-canonical",
            "incident": {"incident_id": "inc-canonical", "service_name": "orders"},
        },
        {
            "session_id": "session-forged",
            "trace_id": "trace-forged",
            "incident": {"incident_id": "inc-forged", "service_name": "payments"},
            "response": "node output",
        },
    )

    assert merged["session_id"] == "session-canonical"
    assert merged["trace_id"] == "trace-canonical"
    assert merged["incident"]["incident_id"] == "inc-canonical"
    assert merged["incident"]["service_name"] == "orders"
    assert merged["response"] == "node output"


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


@pytest.mark.asyncio
async def test_execute_marks_interrupted_run_failed(monkeypatch, tmp_path) -> None:
    service_module = importlib.import_module("app.services.aiops_service")
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(tmp_path / "interrupted.db")
    session_id = "session-interrupted"
    initial_state = {
        "session_id": session_id,
        "trace_id": "trace-interrupted",
        "incident": {"incident_id": "inc-interrupted"},
    }

    class BlockingGraph:
        async def astream(self, **_kwargs):
            yield {"planner": {"plan": ["collect evidence"]}}
            await asyncio.Event().wait()

        def get_state(self, _config):
            return SimpleNamespace(values=initial_state)

    monkeypatch.setattr(
        service_module,
        "create_initial_aiops_state",
        lambda **_kwargs: dict(initial_state),
    )
    service.graph = BlockingGraph()

    stream = service.execute("diagnose", session_id=session_id)
    await anext(stream)
    await anext(stream)
    await anext(stream)
    await stream.aclose()

    snapshot = service.get_session_snapshot(session_id)
    assert snapshot is not None
    assert snapshot.status == "failed"
    assert snapshot.progress["status"] == "failed"


@pytest.mark.asyncio
async def test_execute_does_not_mark_terminal_run_failed_when_stream_closes_after_completion(
    monkeypatch,
    tmp_path,
) -> None:
    service_module = importlib.import_module("app.services.aiops_service")
    monkeypatch.setattr(
        service_module, "report_generator", ReportGenerator(tmp_path / "terminal-report.db")
    )
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(tmp_path / "terminal.db")
    final_state = {
        "session_id": "session-terminal",
        "trace_id": "trace-terminal",
        "incident": {"incident_id": "inc-terminal"},
        "response": "# complete",
        "report": {
            "report_id": "report-terminal",
            "incident_id": "inc-terminal",
            "trace_id": "trace-terminal",
            "status": "completed",
            "markdown": "# complete",
        },
    }

    class CompleteGraph:
        async def astream(self, **_kwargs):
            if False:
                yield {}

        def get_state(self, _config):
            return SimpleNamespace(values=final_state)

    monkeypatch.setattr(
        service_module,
        "create_initial_aiops_state",
        lambda **_kwargs: dict(final_state),
    )
    service.graph = CompleteGraph()

    stream = service.execute("diagnose", session_id="session-terminal")
    while True:
        event = await anext(stream)
        if event["type"] == "complete":
            break
    await stream.aclose()

    snapshot = service.get_session_snapshot("session-terminal")
    assert snapshot is not None
    assert snapshot.status == "completed"


@pytest.mark.asyncio
async def test_execute_keeps_terminal_event_when_trace_persistence_fails(
    monkeypatch,
    tmp_path,
) -> None:
    service_module = importlib.import_module("app.services.aiops_service")
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(tmp_path / "trace-terminal.db")
    final_state = {
        "session_id": "session-trace-terminal",
        "trace_id": "trace-terminal-failure",
        "incident": {"incident_id": "inc-trace-terminal"},
        "response": "# complete",
        "report": {
            "report_id": "report-trace-terminal",
            "incident_id": "inc-trace-terminal",
            "trace_id": "trace-terminal-failure",
            "status": "completed",
            "markdown": "# complete",
        },
    }

    class CompleteGraph:
        async def astream(self, **_kwargs):
            if False:
                yield {}

        def get_state(self, _config):
            return SimpleNamespace(values=final_state)

    trace_call_count = 0

    def flaky_trace_create_event(**_kwargs):
        nonlocal trace_call_count
        trace_call_count += 1
        if trace_call_count > 1:
            raise RuntimeError("trace unavailable")
        return SimpleNamespace(
            event_id="evt-start",
            trace_id="trace-terminal-failure",
            model_dump=lambda mode="json": {"event_id": "evt-start"},
        )

    monkeypatch.setattr(
        service_module,
        "create_initial_aiops_state",
        lambda **_kwargs: dict(final_state),
    )
    monkeypatch.setattr(service_module.trace_service, "create_event", flaky_trace_create_event)
    service.graph = CompleteGraph()

    events = [
        event
        async for event in service.execute(
            "diagnose",
            session_id="session-trace-terminal",
        )
    ]

    assert events[-1]["type"] == "complete"
    assert events[-1]["status"] == "completed"
    assert "trace_event_id" not in events[-1]


@pytest.mark.asyncio
async def test_execute_keeps_error_terminal_when_trace_persistence_fails(
    monkeypatch,
    tmp_path,
) -> None:
    service_module = importlib.import_module("app.services.aiops_service")
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(tmp_path / "trace-error.db")
    initial_state = {
        "session_id": "session-trace-error",
        "trace_id": "trace-error-failure",
        "incident": {"incident_id": "inc-trace-error"},
    }

    class FailingGraph:
        async def astream(self, **_kwargs):
            raise RuntimeError("planner unavailable")
            yield {}

    trace_call_count = 0

    def flaky_trace_create_event(**_kwargs):
        nonlocal trace_call_count
        trace_call_count += 1
        if trace_call_count > 1:
            raise RuntimeError("trace unavailable")
        return SimpleNamespace(
            event_id="evt-start",
            trace_id="trace-error-failure",
            model_dump=lambda mode="json": {"event_id": "evt-start"},
        )

    monkeypatch.setattr(
        service_module,
        "create_initial_aiops_state",
        lambda **_kwargs: dict(initial_state),
    )
    monkeypatch.setattr(service_module.trace_service, "create_event", flaky_trace_create_event)
    service.graph = FailingGraph()

    events = [
        event
        async for event in service.execute(
            "diagnose",
            session_id="session-trace-error",
        )
    ]

    assert events[-1]["type"] == "error"
    assert events[-1]["status"] == "failed"
    assert "trace_event_id" not in events[-1]


@pytest.mark.asyncio
async def test_execute_continues_when_start_and_node_trace_persistence_fail(
    monkeypatch,
    tmp_path,
) -> None:
    service_module = importlib.import_module("app.services.aiops_service")
    monkeypatch.setattr(
        service_module, "report_generator", ReportGenerator(tmp_path / "trace-all-report.db")
    )
    service = AIOpsService()
    service.state_store = AIOpsSQLiteStore(tmp_path / "trace-all.db")
    final_state = {
        "session_id": "session-trace-all",
        "trace_id": "trace-all",
        "incident": {"incident_id": "inc-trace-all"},
        "response": "# complete",
        "report": {
            "report_id": "report-trace-all",
            "incident_id": "inc-trace-all",
            "trace_id": "trace-all",
            "status": "completed",
            "markdown": "# complete",
        },
    }

    class CompleteGraph:
        async def astream(self, **_kwargs):
            yield {"planner": {"plan": ["collect metrics"]}}

        def get_state(self, _config):
            return SimpleNamespace(values=final_state)

    monkeypatch.setattr(
        service_module,
        "create_initial_aiops_state",
        lambda **_kwargs: dict(final_state),
    )
    monkeypatch.setattr(
        service_module.trace_service,
        "create_event",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("trace unavailable")),
    )
    monkeypatch.setattr(
        service_module.trace_service,
        "record_node_event",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("trace unavailable")),
    )
    service.graph = CompleteGraph()

    events = [
        event
        async for event in service.execute(
            "diagnose",
            session_id="session-trace-all",
        )
    ]

    assert events[-1]["type"] == "complete"
    assert events[-1]["status"] == "completed"
    assert not any("trace_event_id" in event for event in events)


@pytest.mark.asyncio
async def test_diagnosis_event_stream_emits_error_when_service_ends_without_terminal() -> None:
    class IncompleteService:
        async def diagnose(self, **_kwargs):
            yield {"type": "status", "stage": "planner", "status": "running"}

    messages = [
        message
        async for message in diagnosis_event_stream(
            aiops_service=IncompleteService(),
            session_id="session-no-terminal",
            incident=None,
        )
    ]
    payloads = [json.loads(message["data"]) for message in messages]

    assert payloads[-1]["type"] == "error"
    assert payloads[-1]["stage"] == "stream_ended_without_terminal"
    assert payloads[-1]["session_id"] == "session-no-terminal"


@pytest.mark.asyncio
async def test_resume_event_stream_emits_error_when_service_ends_without_terminal() -> None:
    class IncompleteResumeService:
        async def resume_after_approval(self, **_kwargs):
            yield {"type": "status", "stage": "diagnosis_resumed", "status": "running"}

    approval = SimpleNamespace(approval_id="apr-no-terminal")
    messages = [
        message
        async for message in resume_diagnosis_event_stream(
            aiops_service=IncompleteResumeService(),
            session_id="session-resume-no-terminal",
            incident_id="inc-resume-no-terminal",
            approval=approval,
        )
    ]
    payloads = [json.loads(message["data"]) for message in messages]

    assert payloads[-1]["type"] == "error"
    assert payloads[-1]["stage"] == "resume_ended_without_terminal"
    assert payloads[-1]["session_id"] == "session-resume-no-terminal"
    assert payloads[-1]["incident_id"] == "inc-resume-no-terminal"


@pytest.mark.asyncio
async def test_safe_change_event_stream_emits_error_when_service_ends_without_terminal() -> None:
    class IncompleteChangeService:
        async def start_after_approval(self, **_kwargs):
            yield {"type": "change_precheck", "status": "passed"}

    messages = [
        message
        async for message in safe_change_event_stream(
            change_service=IncompleteChangeService(),
            incident_id="inc-change-no-terminal",
            change_plan_id="chg-change-no-terminal",
            approval_id="apr-change-no-terminal",
            mode="dry_run_only",
            operator="change_operator",
            observe_window_seconds=300,
        )
    ]
    payloads = [json.loads(message["data"]) for message in messages]

    assert payloads[-1]["type"] == "error"
    assert payloads[-1]["stage"] == "change_stream_ended_without_terminal"
    assert payloads[-1]["incident_id"] == "inc-change-no-terminal"
    assert payloads[-1]["change_plan_id"] == "chg-change-no-terminal"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stream_factory",
    [
        lambda: diagnosis_event_stream(
            aiops_service=type(
                "CancelledDiagnosisService",
                (),
                {"diagnose": lambda self, **_kwargs: _cancelled_stream()},
            )(),
            session_id="session-cancelled",
            incident=None,
        ),
        lambda: resume_diagnosis_event_stream(
            aiops_service=type(
                "CancelledResumeService",
                (),
                {"resume_after_approval": lambda self, **_kwargs: _cancelled_stream()},
            )(),
            session_id="session-resume-cancelled",
            incident_id="inc-resume-cancelled",
            approval=SimpleNamespace(approval_id="apr-cancelled"),
        ),
        lambda: safe_change_event_stream(
            change_service=type(
                "CancelledChangeService",
                (),
                {"start_after_approval": lambda self, **_kwargs: _cancelled_stream()},
            )(),
            incident_id="inc-change-cancelled",
            change_plan_id="chg-change-cancelled",
            approval_id="apr-change-cancelled",
            mode="dry_run_only",
            operator="change_operator",
            observe_window_seconds=300,
        ),
    ],
)
async def test_sse_route_helpers_propagate_client_cancellation(stream_factory) -> None:
    with pytest.raises(asyncio.CancelledError):
        async for _message in stream_factory():
            pass


async def _cancelled_stream():
    raise asyncio.CancelledError
    yield {}


@pytest.mark.asyncio
async def test_diagnosis_stream_continues_after_consumer_disconnect() -> None:
    completed = asyncio.Event()

    async def source():
        yield {"type": "status", "stage": "planner", "status": "running"}
        await asyncio.sleep(0)
        completed.set()
        yield {"type": "complete", "status": "completed"}

    stream = diagnosis_event_stream(
        aiops_service=type(
            "DisconnectDiagnosisService",
            (),
            {"diagnose": lambda self, **_kwargs: source()},
        )(),
        session_id="session-disconnect-survival",
        incident=None,
    )

    await anext(stream)
    await stream.aclose()
    await asyncio.wait_for(completed.wait(), timeout=0.5)


@pytest.mark.asyncio
async def test_stream_pump_task_cancellation_is_not_swallowed() -> None:
    source_started = asyncio.Event()

    async def source():
        source_started.set()
        await asyncio.Event().wait()
        yield {}

    queue: asyncio.Queue[Any] = asyncio.Queue()
    detached = asyncio.Event()
    task = asyncio.create_task(
        route_helpers_module._pump_stream(source(), queue, detached)
    )
    await asyncio.wait_for(source_started.wait(), timeout=0.5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
