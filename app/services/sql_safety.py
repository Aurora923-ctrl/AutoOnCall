"""Small guards for the limited SQL fragments that cannot be bound as values."""

from __future__ import annotations

from collections.abc import Collection
from typing import Literal

BindMarker = Literal["?", "%s"]


def bind_markers(count: int, marker: BindMarker) -> str:
    """Build a non-empty parameter-marker list without accepting SQL text."""
    if count < 1:
        raise ValueError("at least one SQL bind marker is required")
    if marker not in {"?", "%s"}:
        raise ValueError("unsupported SQL bind marker")
    return ", ".join(marker for _ in range(count))


def trusted_identifier(identifier: str, *, allowed: Collection[str]) -> str:
    """Return an identifier only when it belongs to a code-owned whitelist."""
    if identifier not in allowed:
        raise ValueError(f"untrusted SQL identifier: {identifier}")
    return identifier


def trusted_in_clause(count: int, marker: BindMarker) -> str:
    """Return a parenthesized IN-list made only from driver bind markers."""
    return f"({bind_markers(count, marker)})"


def trusted_table_statement(
    operation: Literal["SELECT_COUNT", "DELETE", "SELECT_ALL_COUNT", "DELETE_ALL"],
    *,
    table: str,
    allowed_tables: Collection[str],
    value_count: int = 0,
    marker: BindMarker = "?",
) -> str:
    """Build retention SQL from an allowlisted table and generated bind markers.

    The targeted ``nosec B608`` annotations live here because Bandit cannot infer
    that ``safe_table`` passed a code-owned whitelist and ``in_clause`` contains
    no values, only driver parameter markers.
    """
    safe_table = trusted_identifier(table, allowed=allowed_tables)
    if operation == "SELECT_ALL_COUNT":
        return f"SELECT COUNT(*) FROM {safe_table}"  # nosec B608
    if operation == "DELETE_ALL":
        return f"DELETE FROM {safe_table}"  # nosec B608
    in_clause = trusted_in_clause(value_count, marker)
    if operation == "SELECT_COUNT":
        return f"SELECT COUNT(*) FROM {safe_table} WHERE incident_id IN {in_clause}"  # nosec B608
    if operation == "DELETE":
        return f"DELETE FROM {safe_table} WHERE incident_id IN {in_clause}"  # nosec B608
    raise ValueError(f"unsupported trusted SQL operation: {operation}")
