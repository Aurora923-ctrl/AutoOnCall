"""Tests for A2A status and payload projection boundaries."""

import pytest

from app.services.a2a_messages import parse_message_envelope
from app.services.a2a_payloads import (
    a2a_state_from_autooncall_status,
    data_part,
    text_part,
)


def test_extended_change_states_map_without_false_completion() -> None:
    assert a2a_state_from_autooncall_status("partial_success") == "TASK_STATE_INPUT_REQUIRED"
    assert a2a_state_from_autooncall_status("recovery_pending") == "TASK_STATE_INPUT_REQUIRED"
    assert a2a_state_from_autooncall_status("rollback_failed") == "TASK_STATE_FAILED"
    assert a2a_state_from_autooncall_status("rolled_back") == "TASK_STATE_COMPLETED"


def test_unknown_and_non_terminal_states_fail_closed() -> None:
    assert a2a_state_from_autooncall_status("created") == "TASK_STATE_WORKING"
    assert a2a_state_from_autooncall_status("resume_running") == "TASK_STATE_WORKING"
    assert a2a_state_from_autooncall_status("needs_human") == "TASK_STATE_INPUT_REQUIRED"
    assert a2a_state_from_autooncall_status("degraded") == "TASK_STATE_INPUT_REQUIRED"
    assert a2a_state_from_autooncall_status("rollback_recommended") == "TASK_STATE_INPUT_REQUIRED"
    assert a2a_state_from_autooncall_status("cancelled") == "TASK_STATE_CANCELED"
    assert a2a_state_from_autooncall_status("future_status") == "TASK_STATE_UNSPECIFIED"
    assert a2a_state_from_autooncall_status(" COMPLETED ") == "TASK_STATE_COMPLETED"


def test_a2a_parts_declare_media_types() -> None:
    assert text_part("hello") == {"text": "hello", "mediaType": "text/plain"}
    assert data_part({"answer": 42}) == {
        "data": {"answer": 42},
        "mediaType": "application/json",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"message": {"messageId": "m" * 257, "parts": []}},
        {"message": {"messageId": "msg-1", "contextId": "i" * 129, "parts": []}},
        {
            "message": {
                "messageId": "msg-1",
                "parts": [{"text": "x"} for _ in range(101)],
            }
        },
        {"message": {"messageId": "msg-\nforged", "parts": []}},
    ],
)
def test_a2a_envelope_rejects_unbounded_or_unsafe_identifiers(payload) -> None:
    with pytest.raises(ValueError):
        parse_message_envelope(payload)
