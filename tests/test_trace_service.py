"""Tests for the AIOps trace service and incident trace API."""

import importlib
import json

import pytest

from app.models.trace import ToolCallRecord, TraceEvent
from app.services.sqlite_store import AIOpsSQLiteStore
from app.services.trace_service import TraceService


def test_trace_service_records_lists_and_reloads_events(tmp_path) -> None:
    path = tmp_path / "traces.db"
    service = TraceService(path)

    node_event = service.create_event(
        trace_id="trace-1",
        incident_id="inc-1",
        node_name="planner",
        event_type="node",
        input_summary="plan incident",
        output_summary="plan_steps=3",
    )
    tool_event = service.record_tool_call(
        ToolCallRecord(
            trace_id="trace-1",
            incident_id="inc-1",
            step_id="s1",
            tool_name="query_metrics",
            input_args={"service_name": "order-service"},
            output={"summary": "P95 high"},
            latency_ms=12.5,
            status="success",
            execution_metadata={
                "retry": {
                    "attempt_count": 2,
                    "retried": True,
                    "stop_reason": "success",
                }
            },
        )
    )

    assert node_event.event_id.startswith("traceevt-")
    assert tool_event.event_type == "tool_call"
    assert tool_event.metadata["execution_metadata"]["retry"]["attempt_count"] == 2
    assert len(service.list_events(incident_id="inc-1")) == 2
    assert (
        service.list_events(incident_id="inc-1", event_type="tool_call")[0].tool_name
        == "query_metrics"
    )

    reloaded = TraceService(path)
    assert len(reloaded.list_events(trace_id="trace-1")) == 2


def test_trace_event_id_is_immutable_on_duplicate_write(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "immutable-trace.db")
    first = TraceEvent(
        event_id="traceevt-stable",
        trace_id="trace-first",
        incident_id="inc-first",
        node_name="planner",
        output_summary="original",
    )
    conflicting = first.model_copy(
        update={
            "trace_id": "trace-conflicting",
            "incident_id": "inc-conflicting",
            "output_summary": "overwritten",
        }
    )

    store.save_trace_event(first)
    with pytest.raises(ValueError, match="already exists with different payload"):
        store.save_trace_event(conflicting)

    saved = store.list_trace_events(trace_id="trace-first")
    assert len(saved) == 1
    assert saved[0].incident_id == "inc-first"
    assert saved[0].output_summary == "original"
    assert store.list_trace_events(trace_id="trace-conflicting") == []


def test_legacy_trace_migration_redacts_sensitive_payloads(tmp_path) -> None:
    legacy_path = tmp_path / "traces.jsonl"
    event = TraceEvent(
        trace_id="trace-legacy-redaction",
        incident_id="inc-legacy-redaction",
        node_name="executor",
        input_summary="authorization=Bearer input-secret",
        output_summary="token=output-secret",
        tool_args={"api_key": "args-secret"},
        tool_result={"password": "result-secret"},
        metadata={"cookie": "metadata-secret"},
    )
    legacy_path.write_text(
        json.dumps({"trace_event": event.model_dump(mode="json")}) + "\n",
        encoding="utf-8",
    )

    migrated = TraceService(
        tmp_path / "migrated.db",
        legacy_storage_path=legacy_path,
    ).list_events(trace_id=event.trace_id)[0]
    serialized = migrated.model_dump_json()

    for secret in (
        "input-secret",
        "output-secret",
        "args-secret",
        "result-secret",
        "metadata-secret",
    ):
        assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_trace_service_redacts_sensitive_tool_args(tmp_path) -> None:
    path = tmp_path / "traces.db"
    service = TraceService(path)

    event = service.record_tool_call(
        ToolCallRecord(
            trace_id="trace-redact",
            incident_id="inc-redact",
            step_id="s1",
            tool_name="query_logs",
            input_args={
                "service_name": "order-service",
                "authorization": "Bearer secret",
                "nested": {"password": "redis-password"},
            },
            status="success",
        )
    )

    assert event.tool_args["service_name"] == "order-service"
    assert event.tool_args["authorization"] == "[REDACTED]"
    assert event.tool_args["nested"]["password"] == "[REDACTED]"

    reloaded = TraceService(path).list_events(trace_id="trace-redact")[0]
    assert reloaded.tool_args["authorization"] == "[REDACTED]"
    assert reloaded.tool_args["nested"]["password"] == "[REDACTED]"


def test_trace_service_redacts_camel_case_sensitive_keys(tmp_path) -> None:
    service = TraceService(tmp_path / "camel-case-redaction.db")

    event = service.record_tool_call(
        ToolCallRecord(
            trace_id="trace-camel-redact",
            incident_id="inc-camel-redact",
            step_id="s1",
            tool_name="query_logs",
            input_args={
                "apiKey": "api-secret",
                "APIKey": "upper-api-secret",
                "accessToken": "access-secret",
                "clientSecret": "client-secret",
                "privateKey": "private-secret",
            },
            status="success",
        )
    )

    assert event.tool_args == {
        "apiKey": "[REDACTED]",
        "APIKey": "[REDACTED]",
        "accessToken": "[REDACTED]",
        "clientSecret": "[REDACTED]",
        "privateKey": "[REDACTED]",
    }


def test_trace_service_redacts_sensitive_tool_output(tmp_path) -> None:
    path = tmp_path / "traces.db"
    service = TraceService(path)

    event = service.record_tool_call(
        ToolCallRecord(
            trace_id="trace-output-redact",
            incident_id="inc-output-redact",
            step_id="s1",
            tool_name="query_logs",
            input_args={"service_name": "order-service"},
            output={
                "summary": "token=summary-secret",
                "lines": [
                    "Authorization: Bearer log-secret",
                    {"message": "cookie=session-secret", "api_key": "raw-key"},
                ],
            },
            output_summary="Bearer summary-secret",
            error_message="password=error-secret",
            status="success",
        )
    )

    assert event.tool_result["summary"] == "token=[REDACTED]"
    assert event.tool_result["lines"][0] == "Authorization: Bearer [REDACTED]"
    assert event.tool_result["lines"][1]["message"] == "cookie=[REDACTED]"
    assert event.tool_result["lines"][1]["api_key"] == "[REDACTED]"
    assert event.output_summary == "Bearer [REDACTED]"

    reloaded = TraceService(path).list_events(trace_id="trace-output-redact")[0]
    assert reloaded.tool_result["lines"][1]["api_key"] == "[REDACTED]"
    assert "summary-secret" not in reloaded.output_summary


@pytest.mark.asyncio
async def test_incident_trace_api_returns_events(monkeypatch, tmp_path) -> None:
    service = TraceService(tmp_path / "traces.db")
    service.create_event(
        trace_id="trace-api",
        incident_id="inc-api",
        node_name="executor",
        event_type="node",
        output_summary="step complete",
    )

    incidents_api = importlib.import_module("app.api.incidents")

    monkeypatch.setattr(incidents_api, "get_trace_service", lambda: service)
    monkeypatch.setattr(
        incidents_api,
        "get_report_generator",
        lambda: type("Reports", (), {"get_report": lambda self, _incident_id: None})(),
    )
    monkeypatch.setattr(
        incidents_api,
        "get_incident_state_store",
        lambda: type("States", (), {"get_incident_state": lambda self, _incident_id: None})(),
    )

    result = await incidents_api.get_incident_trace("inc-api")

    assert result["incident_id"] == "inc-api"
    assert result["trace_id"] == "trace-api"
    assert result["items"][0]["event_type"] == "node"

    filtered = await incidents_api.get_incident_trace("inc-api", event_type="tool_call")

    assert filtered["incident_id"] == "inc-api"
    assert filtered["trace_id"] == "trace-api"
    assert filtered["items"] == []
