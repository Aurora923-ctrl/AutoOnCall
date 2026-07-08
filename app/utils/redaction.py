"""Shared helpers for redacting secrets before persistence or logs."""

from __future__ import annotations

import re
from typing import Any

REDACTED_VALUE = "[REDACTED]"

SENSITIVE_KEYWORDS = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "key",
    "dsn",
    "authorization",
    "cookie",
    "credential",
    "bearer",
)

_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_AUTH_HEADER_VALUE_RE = re.compile(
    r"\b(authorization)\s*([:=])\s*(?:bearer|basic)?\s*[^,\s;&]+",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|"
    r"authorization|cookie|credential|dsn)\b\s*([=:])\s*(?!Bearer\b)([^,\s;&]+)"
)


def is_sensitive_key(key: str) -> bool:
    """Return True when a mapping key likely contains a secret."""
    normalized = key.strip().lower().replace("-", "_").replace(".", "_")
    return any(keyword in normalized for keyword in SENSITIVE_KEYWORDS)


def redact_sensitive_data(
    value: Any,
    *,
    redact_auth_scheme: bool = False,
    max_string_length: int | None = None,
) -> Any:
    """Recursively redact sensitive values from dict/list/string payloads."""
    if isinstance(value, dict):
        return {
            key: (
                REDACTED_VALUE
                if is_sensitive_key(str(key))
                else redact_sensitive_data(
                    item,
                    redact_auth_scheme=redact_auth_scheme,
                    max_string_length=max_string_length,
                )
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            redact_sensitive_data(
                item,
                redact_auth_scheme=redact_auth_scheme,
                max_string_length=max_string_length,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            redact_sensitive_data(
                item,
                redact_auth_scheme=redact_auth_scheme,
                max_string_length=max_string_length,
            )
            for item in value
        ]
    if isinstance(value, str):
        text = redact_sensitive_text(value, redact_auth_scheme=redact_auth_scheme)
        if max_string_length is not None and len(text) > max_string_length:
            return text[:max_string_length]
        return text
    return value


def redact_sensitive_text(text: str | None, *, redact_auth_scheme: bool = False) -> str:
    """Redact inline secret assignments and bearer tokens from text."""
    value = str(text or "")
    if redact_auth_scheme:
        value = _AUTH_HEADER_VALUE_RE.sub(
            lambda match: f"{match.group(1)}{match.group(2)} {REDACTED_VALUE}",
            value,
        )
        value = _BEARER_PATTERN.sub(f"Bearer {REDACTED_VALUE}", value)
    else:
        value = _BEARER_PATTERN.sub(f"Bearer {REDACTED_VALUE}", value)
    return _SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)} {REDACTED_VALUE}"
            if redact_auth_scheme and match.group(1).lower() == "authorization"
            else f"{match.group(1)}{match.group(2)}{REDACTED_VALUE}"
        ),
        value,
    )
