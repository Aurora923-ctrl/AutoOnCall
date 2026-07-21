"""Shared resource ownership checks for authenticated API callers."""

from __future__ import annotations

from typing import Any

from app.core.auth import ADMIN_SCOPE, AuthPrincipal, can_access_session


def principal_or_anonymous(principal: Any) -> AuthPrincipal:
    return principal if isinstance(principal, AuthPrincipal) else AuthPrincipal(enabled=False)


def owns_session(principal: AuthPrincipal, session_id: str | None) -> bool:
    principal = principal_or_anonymous(principal)
    if not session_id:
        return not principal.enabled or principal.has_scope(ADMIN_SCOPE)
    return can_access_session(principal, session_id)


def owns_incident(principal: AuthPrincipal, state_store: Any, incident_id: str) -> bool:
    principal = principal_or_anonymous(principal)
    if not principal.enabled or principal.has_scope(ADMIN_SCOPE):
        return True
    state = state_store.get_incident_state(incident_id)
    return bool(state and owns_session(principal, state.session_id))


def owns_approval(principal: AuthPrincipal, approval: Any) -> bool:
    principal = principal_or_anonymous(principal)
    if not principal.enabled or principal.has_scope(ADMIN_SCOPE):
        return True
    metadata = getattr(approval, "metadata", {}) or {}
    session_id = str(metadata.get("session_id") or "")
    return bool(session_id and owns_session(principal, session_id))


def owns_incident_or_approval(
    principal: AuthPrincipal,
    state_store: Any,
    approval_service: Any,
    incident_id: str,
) -> bool:
    principal = principal_or_anonymous(principal)
    if not principal.enabled or principal.has_scope(ADMIN_SCOPE):
        return True
    if owns_incident(principal, state_store, incident_id):
        return True
    return any(
        owns_approval(principal, approval)
        for approval in approval_service.list_requests(incident_id=incident_id)
    )


def owns_change_execution(principal: AuthPrincipal, approval_service: Any, execution: Any) -> bool:
    principal = principal_or_anonymous(principal)
    if not principal.enabled or principal.has_scope(ADMIN_SCOPE):
        return True
    try:
        approval = approval_service.get_request(execution.approval_id)
    except Exception:
        return False
    return owns_approval(principal, approval)
