"""Incoming A2A message parsing and request normalization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from app.models.incident import Incident
from app.services.a2a_skills import (
    SKILL_ANSWER_RUNBOOK_QUESTION,
    SKILL_DIAGNOSE_INCIDENT,
    SKILL_EXPLAIN_INCIDENT_REPLAY,
    SKILL_GET_INCIDENT_STATUS,
)

A2A_MESSAGE_ID_MAX_LENGTH = 256
A2A_TASK_ID_MAX_LENGTH = 256
A2A_CONTEXT_ID_MAX_LENGTH = 128
A2A_TEXT_MAX_LENGTH = 8000
A2A_PARTS_MAX_ITEMS = 100
A2A_STRUCTURED_PAYLOAD_MAX_CHARS = 256_000


@dataclass(frozen=True)
class A2AEnvelope:
    """Normalized incoming A2A message payload."""

    message_id: str
    task_id: str
    context_id: str
    text: str
    data: dict[str, Any]
    metadata: dict[str, Any]
    request_fingerprint: str


def parse_message_envelope(payload: dict[str, Any]) -> A2AEnvelope:
    """Accept A2A HTTP+JSON and simple JSON-RPC-like payloads."""
    if not isinstance(payload, dict):
        raise ValueError("A2A request body must be an object")
    if (
        len(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        > A2A_STRUCTURED_PAYLOAD_MAX_CHARS
    ):
        raise ValueError("A2A request body is too large")
    raw_params = payload.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else payload
    raw_message = params.get("message")
    message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else params
    role = str(message.get("role") or params.get("role") or "").strip().upper()
    if role and role not in {"ROLE_USER", "USER"}:
        raise ValueError("A2A message role must identify the caller as a user")
    raw_parts = message.get("parts")
    parts: list[Any] = raw_parts if isinstance(raw_parts, list) else []
    if len(parts) > A2A_PARTS_MAX_ITEMS:
        raise ValueError(f"A2A message parts must contain at most {A2A_PARTS_MAX_ITEMS} items")
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
        or ""
    ).strip()
    if not message_id:
        raise ValueError("A2A messageId is required")
    task_id = str(
        message.get("taskId")
        or message.get("task_id")
        or params.get("taskId")
        or params.get("task_id")
        or data.get("task_id")
        or data.get("session_id")
        or ""
    ).strip()
    context_id = str(
        message.get("contextId")
        or message.get("context_id")
        or params.get("contextId")
        or params.get("context_id")
        or data.get("context_id")
        or data.get("incident_id")
        or ""
    ).strip()
    _validate_identifier("messageId", message_id, A2A_MESSAGE_ID_MAX_LENGTH)
    _validate_identifier("taskId", task_id, A2A_TASK_ID_MAX_LENGTH, required=False)
    _validate_identifier("contextId", context_id, A2A_CONTEXT_ID_MAX_LENGTH, required=False)
    if len(text) > A2A_TEXT_MAX_LENGTH:
        raise ValueError(f"A2A message text must contain at most {A2A_TEXT_MAX_LENGTH} characters")
    if _contains_control_characters(text):
        raise ValueError("A2A message text contains control characters")
    return A2AEnvelope(
        message_id=message_id,
        task_id=task_id,
        context_id=context_id,
        text=text,
        data=data,
        metadata=metadata,
        request_fingerprint=_request_fingerprint(
            text=text,
            data=data,
            metadata=metadata,
            task_id=task_id,
            context_id=context_id,
        ),
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
    return task_id_for_envelope("diagnosis", envelope)


def task_id_for_envelope(kind: str, envelope: A2AEnvelope) -> str:
    """Return a stable server-owned task id for one caller message."""
    message_id = envelope.message_id.strip()
    if not message_id:
        raise ValueError("A2A messageId is required")
    digest = sha256(f"{kind}\0{message_id}".encode()).hexdigest()
    return f"a2a-{kind}-{digest}"


def scope_message_to_principal(payload: dict[str, Any], principal_id: str) -> dict[str, Any]:
    """Namespace caller message identity without changing the business payload."""
    envelope = parse_message_envelope(payload)
    scoped = dict(payload)
    raw_params = scoped.get("params")
    params = dict(raw_params) if isinstance(raw_params, dict) else scoped
    raw_message = params.get("message")
    message = dict(raw_message) if isinstance(raw_message, dict) else params
    message["messageId"] = f"principal:{principal_id}:{envelope.message_id}"
    message_metadata = dict(message.get("metadata") or {})
    message_metadata["__autooncall_principal"] = principal_id
    message["metadata"] = message_metadata
    message.pop("message_id", None)
    if isinstance(raw_message, dict):
        params["message"] = message
    if isinstance(raw_params, dict):
        scoped["params"] = params
    else:
        scoped = params
    return scoped


def _request_fingerprint(
    *,
    text: str,
    data: dict[str, Any],
    metadata: dict[str, Any],
    task_id: str,
    context_id: str,
) -> str:
    payload = {
        "text": text,
        "data": data,
        "metadata": metadata,
        "task_id": task_id,
        "context_id": context_id,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(serialized.encode("utf-8")).hexdigest()


def incident_from_envelope(envelope: A2AEnvelope) -> Incident:
    """Build the structured Incident for a diagnosis request."""
    raw_incident = envelope.data.get("incident")
    if isinstance(raw_incident, dict):
        incident_payload = dict(raw_incident)
        if envelope.context_id:
            incident_id = str(incident_payload.get("incident_id") or "").strip()
            if incident_id and incident_id != envelope.context_id:
                raise ValueError("A2A contextId does not match incident_id")
            incident_payload["incident_id"] = envelope.context_id
        raw_alert = _mapping(incident_payload.get("raw_alert"))
        raw_alert.update(
            {
                "source": "a2a",
                "message_id": envelope.message_id,
                "context_id": envelope.context_id,
                "request_fingerprint": envelope.request_fingerprint,
            }
        )
        incident_payload["raw_alert"] = raw_alert
        return Incident.model_validate(incident_payload)
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


def _validate_identifier(
    field_name: str,
    value: str,
    max_length: int,
    *,
    required: bool = True,
) -> None:
    if not value:
        if required:
            raise ValueError(f"A2A {field_name} is required")
        return
    if len(value) > max_length:
        raise ValueError(f"A2A {field_name} must contain at most {max_length} characters")
    if _contains_control_characters(value):
        raise ValueError(f"A2A {field_name} contains control characters")


def _contains_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


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
