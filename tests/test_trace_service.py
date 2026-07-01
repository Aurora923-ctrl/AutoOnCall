"""Tests for the AIOps trace service and incident trace API."""

import importlib

import pytest

from app.models.trace import ToolCallRecord
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
        )
    )

    assert node_event.event_id.startswith("traceevt-")
    assert tool_event.event_type == "tool_call"
    assert len(service.list_events(incident_id="inc-1")) == 2
    assert service.list_events(incident_id="inc-1", event_type="tool_call")[0].tool_name == "query_metrics"

    reloaded = TraceService(path)
    assert len(reloaded.list_events(trace_id="trace-1")) == 2


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

    result = await incidents_api.get_incident_trace("inc-api")

    assert result["incident_id"] == "inc-api"
    assert result["trace_id"] == "trace-api"
    assert result["items"][0]["event_type"] == "node"
