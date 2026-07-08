"""Shared accessors for AIOps state dictionaries and model-like objects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.utils.structured_data import as_dict, dict_list


def as_mapping(value: Any) -> dict[str, Any]:
    """Return a JSON-safe mapping view for dict or Pydantic-like objects."""
    return as_dict(value)


def incident_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the incident payload from a LangGraph state snapshot."""
    return as_mapping(state.get("incident"))


def incident_field(state: Mapping[str, Any], field_name: str, default: Any = "") -> Any:
    """Read one incident field from a state snapshot with a stable fallback."""
    incident = state.get("incident") or {}
    if isinstance(incident, dict):
        return incident.get(field_name) or default
    return getattr(incident, field_name, default) or default


def extract_incident_id(state: Mapping[str, Any]) -> str:
    """Extract incident_id from state values without assuming model instances."""
    return str(incident_field(state, "incident_id", "incident-unknown"))


def state_dict_list(value: Any) -> list[dict[str, Any]]:
    """Normalize a list containing dict or Pydantic-like objects into dictionaries."""
    return dict_list(value)
