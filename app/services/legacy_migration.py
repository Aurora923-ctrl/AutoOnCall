"""Helpers for migrating legacy AIOps JSONL storage files."""

from __future__ import annotations

from pathlib import Path


def resolve_legacy_jsonl_path(storage_path: Path | None, filename: str) -> Path | None:
    """Return the legacy JSONL path used before SQLite-backed storage."""
    if storage_path is None:
        return Path("logs") / filename
    if storage_path.suffix.lower() == ".jsonl":
        return storage_path
    return None
