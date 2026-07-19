"""Lightweight internal API-token RBAC dependencies."""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import config

READ_SCOPE = "read"
DIAGNOSE_SCOPE = "diagnose"
CHAT_WRITE_SCOPE = "chat_write"
KNOWLEDGE_WRITE_SCOPE = "knowledge_write"
APPROVE_SCOPE = "approve"
CHANGE_SCOPE = "change"
EVAL_SCOPE = "eval"
ADMIN_SCOPE = "admin"

ALL_SCOPES = {
    READ_SCOPE,
    DIAGNOSE_SCOPE,
    CHAT_WRITE_SCOPE,
    KNOWLEDGE_WRITE_SCOPE,
    APPROVE_SCOPE,
    CHANGE_SCOPE,
    EVAL_SCOPE,
}

ROLE_SCOPES = {
    "viewer": {READ_SCOPE},
    "reader": {READ_SCOPE},
    "operator": {READ_SCOPE, DIAGNOSE_SCOPE, CHAT_WRITE_SCOPE, KNOWLEDGE_WRITE_SCOPE, EVAL_SCOPE},
    "approver": {READ_SCOPE, APPROVE_SCOPE},
    "change_operator": {READ_SCOPE, CHANGE_SCOPE},
    "admin": {ADMIN_SCOPE, *ALL_SCOPES},
}

bearer_scheme = HTTPBearer(auto_error=False)
_SAFE_TOKEN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_PLACEHOLDER_TOKEN_RE = re.compile(
    r"^(?:replace[-_ ]?with|change[-_ ]?me|your[-_ ]?|example|sample|dummy|placeholder)",
    re.IGNORECASE,
)
_MIN_TOKEN_LENGTH = 16


@dataclass(frozen=True)
class AuthPrincipal:
    """Authenticated caller context used by protected routes."""

    enabled: bool
    token_name: str = "anonymous"
    principal_id: str = "anonymous"
    scopes: frozenset[str] = frozenset()

    def has_scope(self, scope: str) -> bool:
        return ADMIN_SCOPE in self.scopes or scope in self.scopes


def audit_actor(principal: AuthPrincipal, fallback: str = "operator") -> str:
    """Return the authenticated actor name for audit fields when auth is enabled."""
    if principal.enabled and principal.token_name:
        return principal.token_name
    return fallback or principal.token_name


def scoped_session_id(principal: AuthPrincipal, session_id: str) -> str:
    """Namespace caller-controlled session IDs by authenticated principal."""
    if not principal.enabled:
        return session_id
    return f"principal:{principal.principal_id}:{session_id}"


def require_scope(scope: str):
    """Return a FastAPI dependency that requires one scope when auth is enabled."""

    async def dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
        x_autooncall_token: str | None = Header(default=None, alias="X-AutoOnCall-Token"),
    ) -> AuthPrincipal:
        return authenticate_request(scope, credentials, x_autooncall_token)

    return dependency


def authenticate_request(
    required_scope: str,
    credentials: HTTPAuthorizationCredentials | None = None,
    x_autooncall_token: str | None = None,
) -> AuthPrincipal:
    """Authenticate one request and enforce the required scope."""
    if not config.api_auth_enabled:
        if required_scope in {APPROVE_SCOPE, CHANGE_SCOPE}:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="approval and change APIs require API authentication",
            )
        return AuthPrincipal(enabled=False)

    token_registry = configured_token_scopes()
    if not token_registry:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API auth is enabled but no API tokens are configured",
        )

    presented_token = _extract_token(credentials, x_autooncall_token)
    if not presented_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    for token_entry in token_registry.values():
        if hmac.compare_digest(presented_token, token_entry["token"]):
            principal = AuthPrincipal(
                enabled=True,
                token_name=token_entry["token_name"],
                principal_id=sha256(token_entry["token"].encode("utf-8")).hexdigest()[:16],
                scopes=frozenset(token_entry["scopes"]),
            )
            if not principal.has_scope(required_scope):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"token lacks required scope: {required_scope}",
                )
            return principal

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid API token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def configured_token_scopes() -> dict[str, dict[str, Any]]:
    """Build the token registry from JSON and convenience role-token fields."""
    registry: dict[str, dict[str, Any]] = {}
    registry.update(_parse_json_token_registry(config.api_auth_tokens))
    _add_role_token(registry, "read_token", config.api_read_token, "viewer")
    _add_role_token(registry, "operator_token", config.api_operator_token, "operator")
    _add_role_token(registry, "approver_token", config.api_approver_token, "approver")
    _add_role_token(registry, "change_token", config.api_change_token, "change_operator")
    _add_role_token(registry, "admin_token", config.api_admin_token, "admin")
    return registry


def _extract_token(
    credentials: HTTPAuthorizationCredentials | None,
    x_autooncall_token: str | None,
) -> str:
    if credentials and credentials.scheme.lower() == "bearer" and credentials.credentials:
        return credentials.credentials.strip()
    return (x_autooncall_token or "").strip()


def _parse_json_token_registry(raw_value: str) -> dict[str, dict[str, Any]]:
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}

    registry: dict[str, dict[str, Any]] = {}
    for token, value in payload.items():
        token_text = str(token).strip()
        if not _is_usable_token(token_text):
            continue
        scopes = _expand_scopes(value)
        if not scopes:
            continue
        token_name = _token_name(value, token_text)
        registry[_token_registry_key(token_text)] = {
            "token": token_text,
            "token_name": token_name,
            "scopes": scopes,
        }
    return registry


def _add_role_token(
    registry: dict[str, dict[str, Any]],
    token_name: str,
    token: str,
    role: str,
) -> None:
    token_text = (token or "").strip()
    if not _is_usable_token(token_text):
        return
    registry[token_name] = {
        "token": token_text,
        "token_name": token_name,
        "scopes": ROLE_SCOPES[role],
    }


def _is_usable_token(token: str) -> bool:
    """Reject empty, weak, or obvious example credentials."""
    return (
        len(token) >= _MIN_TOKEN_LENGTH
        and not any(character.isspace() or ord(character) < 32 for character in token)
        and _PLACEHOLDER_TOKEN_RE.match(token) is None
    )


def _token_name(value: Any, token: str) -> str:
    fallback = f"json_token_{sha256(token.encode('utf-8')).hexdigest()[:12]}"
    if not isinstance(value, dict) or not value.get("name"):
        return fallback
    candidate = str(value["name"]).strip()
    return candidate if _SAFE_TOKEN_NAME_RE.fullmatch(candidate) else fallback


def _token_registry_key(token: str) -> str:
    """Return an internal key that cannot collide on a caller-supplied audit name."""
    return f"json_token_{sha256(token.encode('utf-8')).hexdigest()}"


def _expand_scopes(value: Any) -> set[str]:
    if isinstance(value, dict):
        return _expand_scopes(value.get("scopes") or value.get("roles") or value.get("role"))
    if isinstance(value, str):
        return _scopes_from_items([value])
    if isinstance(value, list):
        return _scopes_from_items(value)
    return set()


def _scopes_from_items(items: list[Any]) -> set[str]:
    scopes: set[str] = set()
    for item in items:
        name = str(item).strip()
        if not name:
            continue
        if name in ROLE_SCOPES:
            scopes.update(ROLE_SCOPES[name])
        elif name in ALL_SCOPES or name == ADMIN_SCOPE:
            scopes.add(name)
    return scopes
