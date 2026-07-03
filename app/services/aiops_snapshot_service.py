"""Durable snapshot persistence helpers for AIOps workflow state."""

from __future__ import annotations

from typing import Any

from app.models.aiops_session import AIOpsSessionSnapshot
from app.services.aiops_store import AIOpsStateStore
from app.services.incident_lifecycle import incident_status_from_runtime_status
from app.services.incident_state_builder import build_incident_state_from_state


def save_session_snapshot(
    store: AIOpsStateStore,
    *,
    session_id: str,
    state: dict[str, Any],
    status: str,
    node_name: str,
) -> None:
    """Persist a durable session snapshot and synchronized incident state."""
    if not state:
        return

    snapshot_state = _snapshot_state_with_existing_identity(
        store,
        session_id=session_id,
        state=state,
    )
    snapshot = AIOpsSessionSnapshot.from_state(
        session_id=session_id,
        state=snapshot_state,
        status=status,
        node_name=node_name,
    )
    store.save_aiops_session_snapshot(snapshot)

    incident_state = build_incident_state_from_state(
        state={
            **snapshot_state,
            "node_name": node_name,
        },
        status=incident_status_from_runtime_status(status),
        status_reason=f"AIOps workflow node={node_name}, status={status}",
        session_id=session_id,
    )
    if incident_state.incident_id and incident_state.incident_id != "incident-unknown":
        store.save_incident_state(incident_state)


def _snapshot_state_with_existing_identity(
    store: AIOpsStateStore,
    *,
    session_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Fill missing trace and incident identity fields from the existing snapshot."""
    snapshot_state = dict(state)
    existing = store.get_aiops_session_snapshot(session_id)
    if existing is None:
        return snapshot_state

    if not snapshot_state.get("trace_id"):
        snapshot_state["trace_id"] = existing.trace_id

    incident_payload = snapshot_state.get("incident")
    if not incident_payload:
        snapshot_state["incident"] = existing.incident or {"incident_id": existing.incident_id}
    elif isinstance(incident_payload, dict) and not incident_payload.get("incident_id"):
        incident_payload["incident_id"] = existing.incident_id

    return snapshot_state
