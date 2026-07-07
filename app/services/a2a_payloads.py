"""A2A task, event, artifact, and part payload builders."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.config import config
from app.models.a2a import (
    A2AArtifact,
    A2ADataPart,
    A2AMessage,
    A2ATaskState,
    A2ATaskStatus,
    A2ATextPart,
)


def normalized_a2a_base_path() -> str:
    """Return configured A2A base path with one leading slash and no trailing slash."""
    return config.normalized_a2a_base_path


def a2a_state_from_autooncall_status(status: str) -> A2ATaskState:
    """Map AutoOnCall lifecycle statuses onto A2A task states."""
    normalized = (status or "").strip()
    if normalized in {"waiting_approval", "approval_approved", "waiting_manual_execution"}:
        return "TASK_STATE_INPUT_REQUIRED"
    if normalized in {"manual_result_required"}:
        return "TASK_STATE_INPUT_REQUIRED"
    if normalized in {"approval_rejected", "approval_cancelled", "blocked", "escalated"}:
        return "TASK_STATE_REJECTED"
    if normalized in {"failed", "precheck_failed", "dry_run_failed"}:
        return "TASK_STATE_FAILED"
    if normalized in {
        "running",
        "planning",
        "executing",
        "diagnosing",
        "investigating",
        "change_prechecking",
        "change_dry_run",
        "change_executing_sandbox",
        "observing",
    }:
        return "TASK_STATE_WORKING"
    if normalized:
        return "TASK_STATE_COMPLETED"
    return "TASK_STATE_UNKNOWN"


def task_status(
    *,
    task_id: str,
    context_id: str,
    state: A2ATaskState,
    text: str,
    timestamp: datetime | str | None = None,
) -> A2ATaskStatus:
    """Build a normalized A2A task status."""
    return A2ATaskStatus(
        state=state,
        timestamp=timestamp or datetime.now(UTC),
        message=A2AMessage(
            messageId=f"{task_id}-{state}",
            role="ROLE_AGENT",
            taskId=task_id,
            contextId=context_id,
            parts=[text_part(text)],
            metadata={"state": state},
        ),
    )


def status_update_event(
    *,
    task_id: str,
    context_id: str,
    state: A2ATaskState,
    message: str,
    final: bool,
) -> dict[str, Any]:
    """Return an A2A streaming status update event."""
    return {
        "taskId": task_id,
        "contextId": context_id,
        "status": dump_a2a(
            task_status(task_id=task_id, context_id=context_id, state=state, text=message)
        ),
        "final": final,
    }


def diagnosis_event_to_a2a_event(
    *,
    task_id: str,
    context_id: str,
    event: dict[str, Any],
) -> dict[str, Any] | None:
    """Translate an AutoOnCall diagnosis SSE event into an A2A stream event."""
    event_type = str(event.get("type") or "")
    message = str(event.get("message") or event.get("stage") or event_type or "diagnosis update")
    if event_type == "report" and isinstance(event.get("structured_report"), dict):
        return {
            "taskId": task_id,
            "contextId": context_id or str(event.get("incident_id") or ""),
            "artifact": dump_a2a(
                mixed_artifact(
                    str(event["structured_report"].get("report_id") or "diagnosis_report"),
                    "Diagnosis Report",
                    text=str(
                        event["structured_report"].get("markdown") or event.get("report") or ""
                    ),
                    data=event["structured_report"],
                )
            ),
            "append": False,
            "lastChunk": False,
        }
    if event_type == "complete":
        state = a2a_state_from_autooncall_status(str(event.get("status") or "completed"))
        return status_update_event(
            task_id=task_id,
            context_id=context_id or str(event.get("incident_id") or ""),
            state=state,
            message=message,
            final=True,
        )
    if event_type == "error":
        return status_update_event(
            task_id=task_id,
            context_id=context_id,
            state="TASK_STATE_FAILED",
            message=message,
            final=True,
        )
    if event_type == "approval_required":
        return status_update_event(
            task_id=task_id,
            context_id=context_id,
            state="TASK_STATE_INPUT_REQUIRED",
            message=message,
            final=False,
        )
    return status_update_event(
        task_id=task_id,
        context_id=context_id,
        state="TASK_STATE_WORKING",
        message=message,
        final=False,
    )


def status_message(run_status: dict[str, Any]) -> str:
    """Return a concise human-readable task status message."""
    status_metadata = run_status.get("status_metadata") or {}
    label = str(status_metadata.get("label") or run_status.get("status") or "unknown")
    title = str(
        (run_status.get("incident") or {}).get("title") or run_status.get("incident_id") or ""
    )
    return f"{label}: {title}".strip(": ")


def mixed_artifact(
    artifact_id: str,
    name: str,
    *,
    text: str,
    data: dict[str, Any],
    description: str = "",
) -> A2AArtifact:
    """Build an artifact that carries both text and structured data."""
    parts = []
    if text:
        parts.append(text_part(text))
    parts.append(data_part(data))
    return A2AArtifact(
        artifactId=artifact_id,
        name=name,
        description=description,
        parts=parts,
    )


def data_artifact(
    artifact_id: str,
    name: str,
    data: dict[str, Any],
    *,
    description: str = "",
) -> A2AArtifact:
    """Build a structured data artifact."""
    return A2AArtifact(
        artifactId=artifact_id,
        name=name,
        description=description,
        parts=[data_part(data)],
    )


def text_part(text: str) -> dict[str, Any]:
    """Return an A2A text part as an alias-safe dict."""
    return A2ATextPart(text=text).model_dump(mode="json", by_alias=True)


def data_part(data: dict[str, Any]) -> dict[str, Any]:
    """Return an A2A data part as an alias-safe dict."""
    return A2ADataPart(data=data).model_dump(mode="json", by_alias=True)


def dump_a2a(value: Any) -> Any:
    """Dump A2A models with camelCase aliases."""
    if isinstance(value, list):
        return [dump_a2a(item) for item in value]
    if isinstance(value, dict):
        return {key: dump_a2a(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value
