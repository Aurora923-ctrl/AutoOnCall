"""A2A protocol facade for AutoOnCall.

This router exposes AutoOnCall as a northbound diagnosis agent. It deliberately
does not expose low-level Tool Registry calls, approval decisions, or production
change execution.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from sse_starlette.sse import EventSourceResponse

from app.config import config
from app.core.auth import (
    DIAGNOSE_SCOPE,
    READ_SCOPE,
    authenticate_request,
    bearer_scheme,
    require_scope,
)
from app.models.a2a import A2A_MEDIA_TYPE
from app.services.a2a_facade import (
    READ_ONLY_SKILLS,
    SKILL_DIAGNOSE_INCIDENT,
    SUPPORTED_A2A_SKILLS,
    a2a_facade,
)

discovery_router = APIRouter()
router = APIRouter()


def ensure_a2a_enabled() -> None:
    """Fail closed unless the A2A adapter is explicitly enabled."""
    if not config.a2a_enabled:
        raise HTTPException(status_code=404, detail="A2A adapter is disabled")


def a2a_json(payload: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    """Return an A2A JSON response with the protocol media type."""
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(payload),
        media_type=A2A_MEDIA_TYPE,
    )


def a2a_sse_message(payload: dict[str, Any]) -> dict[str, str]:
    """Return one SSE event carrying an A2A stream payload."""
    return {
        "event": "message",
        "data": json.dumps(payload, ensure_ascii=False, default=str),
    }


def required_scope_for_message(payload: dict[str, Any]) -> str:
    """Return the smallest API scope needed for one A2A message."""
    skill = a2a_facade.requested_skill(payload)
    if skill in READ_ONLY_SKILLS:
        return READ_SCOPE
    if skill == SKILL_DIAGNOSE_INCIDENT:
        return DIAGNOSE_SCOPE
    if skill in SUPPORTED_A2A_SKILLS:
        return DIAGNOSE_SCOPE
    raise ValueError(f"Unsupported A2A skill: {skill}")


def authenticate_a2a_message(
    payload: dict[str, Any],
    credentials: HTTPAuthorizationCredentials | None,
    x_autooncall_token: str | None,
) -> None:
    """Authenticate an A2A message after resolving its business skill."""
    scope = required_scope_for_message(payload)
    authenticate_request(scope, credentials, x_autooncall_token)


@discovery_router.get("/.well-known/agent-card.json")
async def get_agent_card() -> JSONResponse:
    """Return the public A2A Agent Card for capability discovery."""
    ensure_a2a_enabled()
    return a2a_json(a2a_facade.agent_card(extended=False))


@router.get("/extendedAgentCard", dependencies=[Depends(require_scope(READ_SCOPE))])
async def get_extended_agent_card() -> JSONResponse:
    """Return the extended Agent Card with examples for authenticated callers."""
    ensure_a2a_enabled()
    return a2a_json(a2a_facade.agent_card(extended=True))


@router.post("/message:send")
async def message_send(
    payload: dict[str, Any],
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_autooncall_token: str | None = Header(default=None, alias="X-AutoOnCall-Token"),
) -> JSONResponse:
    """Handle A2A message:send."""
    ensure_a2a_enabled()
    try:
        authenticate_a2a_message(payload, credentials, x_autooncall_token)
        return a2a_json(await a2a_facade.send_message(payload))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/message:stream")
async def message_stream(
    payload: dict[str, Any],
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_autooncall_token: str | None = Header(default=None, alias="X-AutoOnCall-Token"),
) -> EventSourceResponse:
    """Handle A2A message:stream using SSE."""
    ensure_a2a_enabled()
    try:
        authenticate_a2a_message(payload, credentials, x_autooncall_token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        try:
            async for event in a2a_facade.stream_message(payload):
                yield a2a_sse_message(event)
        except LookupError as exc:
            yield a2a_sse_message({"error": {"code": "not_found", "message": str(exc)}})
        except ValueError as exc:
            yield a2a_sse_message({"error": {"code": "bad_request", "message": str(exc)}})

    return EventSourceResponse(event_generator(), media_type="text/event-stream")


@router.get("/tasks/{task_id}", dependencies=[Depends(require_scope(READ_SCOPE))])
async def get_task(task_id: str) -> JSONResponse:
    """Return a current A2A task view backed by an AutoOnCall diagnosis run."""
    ensure_a2a_enabled()
    try:
        return a2a_json(a2a_facade.get_task(task_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/tasks", dependencies=[Depends(require_scope(READ_SCOPE))])
async def list_tasks(
    incident_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    """Return recent A2A task views."""
    ensure_a2a_enabled()
    return a2a_json(a2a_facade.list_tasks(incident_id=incident_id, limit=limit))
