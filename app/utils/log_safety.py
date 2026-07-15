"""Helpers for logging user-controlled text without storing raw content."""

from __future__ import annotations

from hashlib import sha256


def sanitize_log_value(value: object, *, max_length: int = 128) -> str:
    """Return a single-line printable representation for a log field."""
    text = str(value or "")
    sanitized = "".join(
        character if character.isprintable() and character not in "\r\n" else "?"
        for character in text
    )
    return sanitized[: max(int(max_length), 1)]


def summarize_text_for_log(value: object, *, label: str = "text") -> str:
    """Return length and hash for user-controlled text."""
    text = str(value or "")
    digest = sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{label}_len={len(text)}, {label}_sha256={digest}"
