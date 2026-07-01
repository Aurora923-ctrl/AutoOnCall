"""Small helpers for Server-Sent Events payload formatting."""

from __future__ import annotations

import json
from typing import Any

TERMINAL_EVENT_TYPES = {"complete", "error"}


def sse_message(payload: dict[str, Any]) -> dict[str, str]:
    """Return a standard SSE message event with JSON-safe data."""
    return {
        "event": "message",
        "data": json.dumps(payload, ensure_ascii=False, default=str),
    }


def is_terminal_event(payload: dict[str, Any]) -> bool:
    """Return True when an event should close the current stream."""
    return str(payload.get("type") or "") in TERMINAL_EVENT_TYPES
