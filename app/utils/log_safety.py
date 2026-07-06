"""Helpers for logging user-controlled text without storing raw content."""

from __future__ import annotations

from hashlib import sha256


def summarize_text_for_log(value: object, *, label: str = "text") -> str:
    """Return length and hash for user-controlled text."""
    text = str(value or "")
    digest = sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{label}_len={len(text)}, {label}_sha256={digest}"
