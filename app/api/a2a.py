"""A2A protocol facade for AutoOnCall.

This router exposes AutoOnCall as a northbound diagnosis agent. It deliberately
does not expose low-level Tool Registry calls, approval decisions, or production
change execution.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.config import config
from app.core.auth import (
    DIAGNOSE_SCOPE,
    READ_SCOPE,
    AuthPrincipal,
    authenticate_request,
    bearer_scheme,
)
from app.models.a2a import A2A_MEDIA_TYPE, A2A_PROTOCOL_VERSION
from app.services.a2a_facade import (
    READ_ONLY_SKILLS,
    SKILL_DIAGNOSE_INCIDENT,
    SUPPORTED_A2A_SKILLS,
    a2a_facade,
    scope_message_to_principal,
)
from app.utils.public_errors import public_exception_message

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
) -> AuthPrincipal:
    """Authenticate an A2A message after resolving its business skill."""
    scope = required_scope_for_message(payload)
    return authenticate_a2a_request(scope, credentials, x_autooncall_token)


def scoped_a2a_payload(payload: dict[str, Any], principal: AuthPrincipal) -> dict[str, Any]:
    """Namespace caller-controlled message identity for authenticated requests."""
    if not principal.enabled:
        return payload
    return scope_message_to_principal(payload, principal.principal_id)


def authenticate_a2a_request(
    scope: str,
    credentials: HTTPAuthorizationCredentials | None,
    x_autooncall_token: str | None,
) -> AuthPrincipal:
    """Require configured authentication for every non-discovery A2A request."""
    if not config.api_auth_enabled:
        raise HTTPException(
            status_code=503,
            detail="A2A requests require API authentication",
        )
    return authenticate_request(scope, credentials, x_autooncall_token)


def a2a_error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    """Return a stable A2A error response."""
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return a2a_json(
        {"error": error},
        status_code=status_code,
    )


def a2a_auth_error_response(exc: HTTPException) -> JSONResponse:
    """Translate authentication failures to the A2A JSON media type."""
    code = {
        401: "unauthorized",
        403: "forbidden",
        503: "authentication_required",
    }.get(exc.status_code, "authentication_error")
    return a2a_error_response(
        status_code=exc.status_code,
        code=code,
        message=str(exc.detail),
    )


def validate_a2a_version(a2a_version: str | None) -> None:
    """Reject an explicitly incompatible A2A protocol version."""
    if a2a_version and a2a_version.strip() != A2A_PROTOCOL_VERSION:
        raise ValueError("Unsupported A2A protocol version")


def a2a_version_error_response(exc: ValueError) -> JSONResponse:
    return a2a_error_response(
        status_code=400,
        code="unsupported_version",
        message=public_exception_message(exc),
        details=[
            {
                "type": "UnsupportedVersion",
                "supportedVersions": [A2A_PROTOCOL_VERSION],
            }
        ],
    )


@discovery_router.get("/.well-known/agent-card.json")
async def get_agent_card() -> JSONResponse:
    """Return the public A2A Agent Card for capability discovery."""
    ensure_a2a_enabled()
    return a2a_json(a2a_facade.agent_card(extended=False))


@router.get("/extendedAgentCard")
async def get_extended_agent_card(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_autooncall_token: str | None = Header(default=None, alias="X-AutoOnCall-Token"),
    a2a_version: str | None = Header(default=None, alias="A2A-Version"),
) -> JSONResponse:
    """Return the extended Agent Card with examples for authenticated callers."""
    ensure_a2a_enabled()
    try:
        validate_a2a_version(a2a_version)
        authenticate_a2a_request(READ_SCOPE, credentials, x_autooncall_token)
    except ValueError as exc:
        return a2a_version_error_response(exc)
    except HTTPException as exc:
        return a2a_auth_error_response(exc)
    return a2a_json(a2a_facade.agent_card(extended=True))


@router.post("/message:send")
async def message_send(
    payload: dict[str, Any],
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_autooncall_token: str | None = Header(default=None, alias="X-AutoOnCall-Token"),
    a2a_version: str | None = Header(default=None, alias="A2A-Version"),
) -> JSONResponse:
    """Handle A2A message:send."""
    ensure_a2a_enabled()
    try:
        validate_a2a_version(a2a_version)
        principal = authenticate_a2a_message(payload, credentials, x_autooncall_token)
        scoped_payload = scoped_a2a_payload(payload, principal)
        return a2a_json(await a2a_facade.send_message(scoped_payload))
    except LookupError as exc:
        return a2a_error_response(
            status_code=404,
            code="not_found",
            message=public_exception_message(exc),
        )
    except ValueError as exc:
        if a2a_version and a2a_version.strip() != A2A_PROTOCOL_VERSION:
            return a2a_version_error_response(exc)
        return a2a_error_response(
            status_code=400,
            code="bad_request",
            message=public_exception_message(exc),
        )
    except HTTPException as exc:
        return a2a_auth_error_response(exc)
    except Exception as exc:
        logger.error(
            "A2A message send failed: error_type={}",
            type(exc).__name__,
            exc_info=True,
        )
        return a2a_error_response(
            status_code=500,
            code="internal_error",
            message=public_exception_message(exc),
        )


@router.post("/message:stream", response_model=None)
async def message_stream(
    payload: dict[str, Any],
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_autooncall_token: str | None = Header(default=None, alias="X-AutoOnCall-Token"),
    a2a_version: str | None = Header(default=None, alias="A2A-Version"),
) -> Response:
    """Handle A2A message:stream using SSE."""
    ensure_a2a_enabled()
    try:
        validate_a2a_version(a2a_version)
        principal = authenticate_a2a_message(payload, credentials, x_autooncall_token)
        scoped_payload = scoped_a2a_payload(payload, principal)
    except HTTPException as exc:
        return a2a_auth_error_response(exc)
    except ValueError as exc:
        if a2a_version and a2a_version.strip() != A2A_PROTOCOL_VERSION:
            return a2a_version_error_response(exc)
        return a2a_error_response(
            status_code=400,
            code="bad_request",
            message=public_exception_message(exc),
        )

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        try:
            async for event in a2a_facade.stream_message(scoped_payload):
                yield a2a_sse_message(event)
        except asyncio.CancelledError:
            raise
        except LookupError as exc:
            yield a2a_sse_message(
                {
                    "error": {"code": "not_found", "message": public_exception_message(exc)},
                    "final": True,
                }
            )
        except ValueError as exc:
            yield a2a_sse_message(
                {
                    "error": {"code": "bad_request", "message": public_exception_message(exc)},
                    "final": True,
                }
            )
        except Exception as exc:
            logger.error(
                "A2A message stream failed: error_type={}",
                type(exc).__name__,
                exc_info=True,
            )
            yield a2a_sse_message(
                {
                    "error": {
                        "code": "internal_error",
                        "message": public_exception_message(exc),
                    },
                    "final": True,
                }
            )

    return EventSourceResponse(event_generator(), media_type="text/event-stream")


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_autooncall_token: str | None = Header(default=None, alias="X-AutoOnCall-Token"),
    a2a_version: str | None = Header(default=None, alias="A2A-Version"),
) -> JSONResponse:
    """Return a current A2A task view backed by an AutoOnCall diagnosis run."""
    ensure_a2a_enabled()
    try:
        validate_a2a_version(a2a_version)
        principal = authenticate_a2a_request(READ_SCOPE, credentials, x_autooncall_token)
        return a2a_json(a2a_facade.get_task(task_id, owner_id=principal.principal_id))
    except HTTPException as exc:
        return a2a_auth_error_response(exc)
    except ValueError as exc:
        return a2a_version_error_response(exc)
    except LookupError as exc:
        return a2a_error_response(
            status_code=404,
            code="not_found",
            message=public_exception_message(exc),
        )
    except Exception as exc:
        logger.error(
            "A2A task lookup failed: error_type={}",
            type(exc).__name__,
            exc_info=True,
        )
        return a2a_error_response(
            status_code=500,
            code="internal_error",
            message=public_exception_message(exc),
        )


@router.get("/tasks")
async def list_tasks(
    incident_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_autooncall_token: str | None = Header(default=None, alias="X-AutoOnCall-Token"),
    a2a_version: str | None = Header(default=None, alias="A2A-Version"),
) -> JSONResponse:
    """Return recent A2A task views."""
    ensure_a2a_enabled()
    try:
        validate_a2a_version(a2a_version)
        principal = authenticate_a2a_request(READ_SCOPE, credentials, x_autooncall_token)
        return a2a_json(
            a2a_facade.list_tasks(
                incident_id=incident_id,
                limit=limit,
                owner_id=principal.principal_id,
            )
        )
    except HTTPException as exc:
        return a2a_auth_error_response(exc)
    except ValueError as exc:
        return a2a_version_error_response(exc)
    except Exception as exc:
        logger.error(
            "A2A task listing failed: error_type={}",
            type(exc).__name__,
            exc_info=True,
        )
        return a2a_error_response(
            status_code=500,
            code="internal_error",
            message=public_exception_message(exc),
        )
