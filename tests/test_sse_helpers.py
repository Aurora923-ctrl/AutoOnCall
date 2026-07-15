"""Tests for API SSE payload helpers."""

import json
from datetime import datetime

from app.api.sse import is_terminal_event, sse_message


def test_sse_message_serializes_non_json_values_safely() -> None:
    event = sse_message({"type": "status", "created_at": datetime(2026, 6, 24, 12, 0, 0)})

    assert event["event"] == "message"
    payload = json.loads(event["data"])
    assert payload["type"] == "status"
    assert payload["created_at"] == "2026-06-24 12:00:00"


def test_sse_terminal_event_detection_is_limited_to_stream_end_types() -> None:
    assert is_terminal_event({"type": "complete"}) is True
    assert is_terminal_event({"type": "done"}) is True
    assert is_terminal_event({"type": "error"}) is True
    assert is_terminal_event({"type": "approval_required"}) is False


def test_sse_message_keeps_terminal_status_contract() -> None:
    event = sse_message(
        {
            "type": "complete",
            "stage": "diagnosis_complete",
            "status": "waiting_approval",
            "structured_report": {"status": "waiting_approval"},
            "diagnosis": {"status": "waiting_approval"},
        }
    )

    payload = json.loads(event["data"])
    assert payload["status"] == "waiting_approval"
    assert payload["structured_report"]["status"] == "waiting_approval"
    assert payload["diagnosis"]["status"] == "waiting_approval"


def test_sse_message_uses_progress_cursor_as_event_id() -> None:
    event = sse_message(
        {
            "type": "progress",
            "progress_cursor": "session-1:000001",
            "phase": "planning",
        }
    )

    assert event["id"] == "session-1:000001"
    payload = json.loads(event["data"])
    assert payload["type"] == "progress"
    assert is_terminal_event(payload) is False


def test_sse_message_preserves_zero_cursor() -> None:
    event = sse_message({"type": "progress", "cursor": 0})

    assert event["id"] == "0"


def test_terminal_event_detection_normalizes_event_type() -> None:
    assert is_terminal_event({"type": " ERROR "}) is True
