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
        store.save_aiops_session_snapshot_with_incident(snapshot, incident_state)
    else:
        store.save_aiops_session_snapshot(snapshot)


def create_session_snapshot(
    store: AIOpsStateStore,
    *,
    session_id: str,
    state: dict[str, Any],
    status: str,
    node_name: str,
) -> bool:
    """Atomically create the first durable snapshot for a diagnosis run."""
    if not state:
        return False
    snapshot = AIOpsSessionSnapshot.from_state(
        session_id=session_id,
        state=state,
        status=status,
        node_name=node_name,
    )
    incident_state = build_incident_state_from_state(
        state={
            **state,
            "node_name": node_name,
        },
        status=incident_status_from_runtime_status(status),
        status_reason=f"AIOps workflow node={node_name}, status={status}",
        session_id=session_id,
    )
    if incident_state.incident_id and incident_state.incident_id != "incident-unknown":
        if not store.create_aiops_session_snapshot_with_incident(snapshot, incident_state):
            return False
    elif not store.create_aiops_session_snapshot(snapshot):
        return False
    return True


def transition_session_snapshot(
    store: AIOpsStateStore,
    *,
    session_id: str,
    state: dict[str, Any],
    status: str,
    node_name: str,
    expected_statuses: set[str],
) -> bool:
    """Atomically transition a durable snapshot from allowed statuses."""
    if not state:
        return False

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
        if not store.update_aiops_session_snapshot_with_incident_if_status(
            snapshot,
            incident_state,
            expected_statuses=expected_statuses,
        ):
            return False
    elif not store.update_aiops_session_snapshot_if_status(
        snapshot,
        expected_statuses=expected_statuses,
    ):
        return False
    return True


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

    _merge_existing_progress(snapshot_state, existing)

    incident_payload = snapshot_state.get("incident")
    if not incident_payload:
        snapshot_state["incident"] = existing.incident or {"incident_id": existing.incident_id}
    elif isinstance(incident_payload, dict) and not incident_payload.get("incident_id"):
        incident_payload["incident_id"] = existing.incident_id

    return snapshot_state


def _merge_existing_progress(
    snapshot_state: dict[str, Any],
    existing: AIOpsSessionSnapshot,
) -> None:
    """Keep progress recovery data when a save path only updates business state."""
    if not snapshot_state.get("progress") and existing.progress:
        snapshot_state["progress"] = dict(existing.progress)
    if not snapshot_state.get("progress_cursor") and existing.progress_cursor:
        snapshot_state["progress_cursor"] = existing.progress_cursor

    incoming_events = _progress_events(snapshot_state.get("progress_events"))
    existing_events = _progress_events(existing.progress_events)
    if not incoming_events:
        if existing_events:
            snapshot_state["progress_events"] = existing_events
        return

    merged_by_cursor: dict[str, dict[str, Any]] = {}
    cursorless: list[dict[str, Any]] = []
    for item in [*existing_events, *incoming_events]:
        cursor = str(item.get("cursor") or "")
        if cursor:
            merged_by_cursor[cursor] = item
        else:
            cursorless.append(item)
    snapshot_state["progress_events"] = [*cursorless, *merged_by_cursor.values()][-20:]


def _progress_events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]
