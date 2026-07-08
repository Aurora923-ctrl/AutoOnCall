"""Small helpers for normalizing JSON-like runtime values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def json_safe(value: Any) -> Any:
    """Convert common Python and Pydantic values into JSON-safe primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(item) for item in value]
    return str(value)


def as_dict(value: Any) -> dict[str, Any]:
    """Return a dictionary for mapping or Pydantic-like values."""
    safe = json_safe(value)
    return dict(safe) if isinstance(safe, Mapping) else {}


def optional_dict(value: Any) -> dict[str, Any] | None:
    """Return a dictionary or None when the normalized value is empty."""
    payload = as_dict(value)
    return payload or None


def dict_list(value: Any, *, wrap_scalars: bool = False) -> list[dict[str, Any]]:
    """Normalize a list into dictionaries, optionally wrapping scalar items."""
    safe = json_safe(value)
    if not isinstance(safe, list):
        return []
    result: list[dict[str, Any]] = []
    for item in safe:
        if isinstance(item, Mapping):
            result.append(dict(item))
        elif wrap_scalars:
            result.append({"value": item})
    return result


def string_list(value: Any) -> list[str]:
    """Normalize a list into strings, unwrapping {'value': item} records."""
    safe = json_safe(value)
    if not isinstance(safe, list):
        return []
    result: list[str] = []
    for item in safe:
        if isinstance(item, Mapping) and set(item.keys()) == {"value"}:
            item = item.get("value")
        if item is not None:
            result.append(str(item))
    return result


def normalize_past_steps(value: Any) -> list[dict[str, Any]]:
    """Normalize legacy past_steps tuples and newer dict entries."""
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            normalized.append({"step": json_safe(item[0]), "result": json_safe(item[1])})
        elif isinstance(item, Mapping):
            normalized.append(as_dict(item))
        else:
            normalized.append({"value": json_safe(item)})
    return normalized


def dedupe_strings(values: list[str]) -> list[str]:
    """Return strings in first-seen order without duplicates."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
