"""Incoming A2A message parsing and request normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.models.incident import Incident
from app.services.a2a_skills import (
    SKILL_ANSWER_RUNBOOK_QUESTION,
    SKILL_DIAGNOSE_INCIDENT,
    SKILL_EXPLAIN_INCIDENT_REPLAY,
    SKILL_GET_INCIDENT_STATUS,
)


@dataclass(frozen=True)
class A2AEnvelope:
    """Normalized incoming A2A message payload."""

    message_id: str
    task_id: str
    context_id: str
    text: str
    data: dict[str, Any]
    metadata: dict[str, Any]


def parse_message_envelope(payload: dict[str, Any]) -> A2AEnvelope:
    """Accept A2A HTTP+JSON and simple JSON-RPC-like payloads."""
    raw_params = payload.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else payload
    raw_message = params.get("message")
    message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else params
    raw_parts = message.get("parts")
    parts: list[Any] = raw_parts if isinstance(raw_parts, list) else []
    data: dict[str, Any] = {}
    data.update(_mapping(params.get("data")))
    data.update(_data_from_parts(parts))
    metadata: dict[str, Any] = {}
    metadata.update(_mapping(params.get("metadata")))
    metadata.update(_mapping(message.get("metadata")))
    metadata.update(_mapping(data.get("metadata")))
    text = str(params.get("text") or message.get("text") or _text_from_parts(parts)).strip()
    message_id = str(
        message.get("messageId")
        or message.get("message_id")
        or params.get("messageId")
        or params.get("message_id")
        or uuid4().hex
    )
    task_id = str(
        message.get("taskId")
        or message.get("task_id")
        or params.get("taskId")
        or params.get("task_id")
        or data.get("task_id")
        or data.get("session_id")
        or ""
    )
    context_id = str(
        message.get("contextId")
        or message.get("context_id")
        or params.get("contextId")
        or params.get("context_id")
        or data.get("context_id")
        or data.get("incident_id")
        or ""
    )
    return A2AEnvelope(
        message_id=message_id,
        task_id=task_id,
        context_id=context_id,
        text=text,
        data=data,
        metadata=metadata,
    )


def resolve_skill(envelope: A2AEnvelope) -> str:
    """Infer the business skill requested by the A2A caller."""
    requested = str(
        envelope.metadata.get("skill")
        or envelope.metadata.get("skill_id")
        or envelope.data.get("skill")
        or envelope.data.get("skill_id")
        or envelope.data.get("intent")
        or ""
    ).strip()
    if requested:
        return requested
    if isinstance(envelope.data.get("incident"), dict):
        return SKILL_DIAGNOSE_INCIDENT
    if envelope.data.get("replay") or envelope.data.get("include_replay"):
        return SKILL_EXPLAIN_INCIDENT_REPLAY
    if envelope.data.get("incident_id") or envelope.task_id:
        return SKILL_GET_INCIDENT_STATUS
    return SKILL_ANSWER_RUNBOOK_QUESTION


def diagnosis_task_id(envelope: A2AEnvelope) -> str:
    """Return a server-owned A2A/AutoOnCall diagnosis task id."""
    return new_task_id("diagnosis")


def new_task_id(kind: str) -> str:
    """Generate a server-owned A2A task id."""
    return f"a2a-{kind}-{uuid4().hex}"


def incident_from_envelope(envelope: A2AEnvelope) -> Incident:
    """Build the structured Incident for a diagnosis request."""
    raw_incident = envelope.data.get("incident")
    if isinstance(raw_incident, dict):
        return Incident.model_validate(raw_incident)
    if envelope.text:
        return Incident(
            title=envelope.text[:120] or "A2A incident diagnosis",
            symptom=envelope.text[:500],
            raw_alert={
                "source": "a2a",
                "message_id": envelope.message_id,
                "context_id": envelope.context_id,
            },
        )
    raise ValueError("diagnose_incident requires an incident object or text symptom")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _data_from_parts(parts: list[Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("kind") or part.get("type") or "")
        if kind == "data" and isinstance(part.get("data"), dict):
            data.update(part["data"])
        elif isinstance(part.get("data"), dict) and not kind:
            data.update(part["data"])
    return data


def _text_from_parts(parts: list[Any]) -> str:
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("kind") or part.get("type") or "")
        if kind == "text" and part.get("text"):
            texts.append(str(part["text"]))
        elif not kind and part.get("text"):
            texts.append(str(part["text"]))
    return "\n".join(texts)
