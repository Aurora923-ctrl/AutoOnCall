"""A2A facade that exposes AutoOnCall as a remote diagnosis agent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.config import config
from app.models.a2a import (
    A2AAgentCard,
    A2AAgentSkill,
    A2AArtifact,
    A2ADataPart,
    A2AMessage,
    A2ATask,
    A2ATaskState,
    A2ATaskStatus,
    A2ATextPart,
)
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.incident import Incident
from app.services.aiops_read_models.common import list_run_trace_events
from app.services.aiops_read_models.run import build_aiops_run_status
from app.services.aiops_service import aiops_service as default_aiops_service
from app.services.aiops_store import create_aiops_store
from app.services.approval_service import approval_service as default_approval_service
from app.services.change_execution_read_models import build_change_execution_read_model
from app.services.change_execution_service import (
    change_execution_service as default_change_execution_service,
)
from app.services.rag_agent_service import rag_agent_service as default_rag_agent_service
from app.services.read_models import build_incident_replay
from app.services.report_generator import report_generator as default_report_generator
from app.services.trace_service import trace_service as default_trace_service

SKILL_DIAGNOSE_INCIDENT = "diagnose_incident"
SKILL_GET_INCIDENT_STATUS = "get_incident_status"
SKILL_EXPLAIN_INCIDENT_REPLAY = "explain_incident_replay"
SKILL_ANSWER_RUNBOOK_QUESTION = "answer_runbook_question"

READ_ONLY_SKILLS = {
    SKILL_GET_INCIDENT_STATUS,
    SKILL_EXPLAIN_INCIDENT_REPLAY,
    SKILL_ANSWER_RUNBOOK_QUESTION,
}

SUPPORTED_A2A_SKILLS = {
    SKILL_DIAGNOSE_INCIDENT,
    *READ_ONLY_SKILLS,
}


class A2AFacade:
    """Translate between A2A protocol objects and AutoOnCall domain services."""

    def __init__(
        self,
        *,
        aiops_service: Any | None = None,
        trace_service: Any | None = None,
        approval_service: Any | None = None,
        report_generator: Any | None = None,
        change_execution_service: Any | None = None,
        rag_agent_service: Any | None = None,
        incident_state_store: Any | None = None,
    ) -> None:
        self.aiops_service = aiops_service or default_aiops_service
        self.trace_service = trace_service or default_trace_service
        self.approval_service = approval_service or default_approval_service
        self.report_generator = report_generator or default_report_generator
        self.change_execution_service = change_execution_service or default_change_execution_service
        self.rag_agent_service = rag_agent_service or default_rag_agent_service
        self.incident_state_store = incident_state_store or create_aiops_store()

    def agent_card(self, *, extended: bool = False) -> dict[str, Any]:
        """Return the A2A Agent Card for capability discovery."""
        base_url = config.normalized_api_base_url
        base_path = config.normalized_a2a_base_path
        agent_url = f"{base_url}{base_path}"
        security_schemes = {}
        security = []
        if config.api_auth_enabled:
            security_schemes = {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "AutoOnCall API token with diagnose/read scope.",
                }
            }
            security = [{"bearerAuth": []}]

        card = A2AAgentCard(
            name=config.a2a_agent_name,
            description=(
                "A2A-compatible OnCall diagnosis agent for incident investigation, "
                "evidence replay, and cited Runbook answers. It does not expose "
                "low-level infrastructure tools or production change execution."
            ),
            url=agent_url,
            supported_interfaces=[
                {"transport": "HTTP+JSON", "url": agent_url},
                {"transport": "SSE", "url": f"{agent_url}/message:stream"},
            ],
            provider={"organization": "AutoOnCall"},
            version=config.app_version,
            documentation_url=f"{base_url}/docs",
            capabilities={
                "streaming": True,
                "pushNotifications": False,
                "stateTransitionHistory": True,
                "extensions": ["autooncall.incident_replay", "autooncall.evidence_artifacts"],
            },
            security_schemes=security_schemes,
            security=security,
            default_input_modes=["text/plain", "application/json"],
            default_output_modes=["application/json", "text/markdown", "text/plain"],
            skills=agent_skills(extended=extended),
        )
        return card.model_dump(mode="json", by_alias=True, exclude_none=True)

    def requested_skill(self, payload: dict[str, Any]) -> str:
        """Return the requested A2A skill without executing it."""
        envelope = parse_message_envelope(payload)
        return resolve_skill(envelope)

    async def send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle A2A message:send and return a final or current task."""
        envelope = parse_message_envelope(payload)
        skill = resolve_skill(envelope)
        if skill == SKILL_DIAGNOSE_INCIDENT:
            return {"task": await self._send_diagnosis(envelope)}
        if skill == SKILL_GET_INCIDENT_STATUS:
            return {"task": self._get_status_task(envelope)}
        if skill == SKILL_EXPLAIN_INCIDENT_REPLAY:
            return {"task": self._get_replay_task(envelope)}
        if skill == SKILL_ANSWER_RUNBOOK_QUESTION:
            return {"task": await self._answer_runbook_question(envelope)}
        raise ValueError(f"Unsupported A2A skill: {skill}")

    async def stream_message(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Handle A2A message:stream."""
        envelope = parse_message_envelope(payload)
        skill = resolve_skill(envelope)
        if skill != SKILL_DIAGNOSE_INCIDENT:
            response = await self.send_message(payload)
            yield {"task": response["task"], "final": True}
            return

        task_id = diagnosis_task_id(envelope)
        incident = incident_from_envelope(envelope)
        context_id = envelope.context_id or incident.incident_id
        yield status_update_event(
            task_id=task_id,
            context_id=context_id,
            state="TASK_STATE_SUBMITTED",
            message="AutoOnCall diagnosis task accepted.",
            final=False,
        )

        last_event: dict[str, Any] = {}
        async for event in self.aiops_service.diagnose(session_id=task_id, incident=incident):
            last_event = dict(event or {})
            converted = diagnosis_event_to_a2a_event(
                task_id=task_id,
                context_id=context_id,
                event=last_event,
            )
            if converted:
                yield converted

        snapshot = self.aiops_service.get_session_snapshot(task_id)
        task = (
            self.task_from_snapshot(snapshot)
            if snapshot is not None
            else self.task_from_terminal_event(task_id, context_id, last_event)
        )
        yield {"task": task, "final": True}

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Return one A2A task from the durable AutoOnCall session snapshot."""
        snapshot = self.aiops_service.get_session_snapshot(task_id)
        if snapshot is None:
            raise LookupError(f"A2A task not found: {task_id}")
        return self.task_from_snapshot(snapshot)

    def list_tasks(self, *, incident_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Return recent A2A task views backed by diagnosis runs."""
        snapshots = self.aiops_service.list_session_snapshots(
            incident_id=incident_id,
            limit=limit,
        )
        return {
            "items": [self.task_from_snapshot(snapshot) for snapshot in snapshots],
            "count": len(snapshots),
        }

    async def _send_diagnosis(self, envelope: A2AEnvelope) -> dict[str, Any]:
        task_id = diagnosis_task_id(envelope)
        incident = incident_from_envelope(envelope)
        last_event: dict[str, Any] = {}
        async for event in self.aiops_service.diagnose(session_id=task_id, incident=incident):
            last_event = dict(event or {})

        snapshot = self.aiops_service.get_session_snapshot(task_id)
        if snapshot is not None:
            return self.task_from_snapshot(snapshot)
        return self.task_from_terminal_event(
            task_id,
            envelope.context_id or incident.incident_id,
            last_event,
        )

    def _get_status_task(self, envelope: A2AEnvelope) -> dict[str, Any]:
        task_id = (
            str(envelope.data.get("task_id") or envelope.data.get("session_id") or "")
            or envelope.task_id
        )
        incident_id = str(envelope.data.get("incident_id") or envelope.context_id or "")
        snapshot = None
        if task_id:
            snapshot = self.aiops_service.get_session_snapshot(task_id)
        if snapshot is None and incident_id:
            snapshots = self.aiops_service.list_session_snapshots(
                incident_id=incident_id,
                limit=1,
            )
            snapshot = snapshots[0] if snapshots else None
        if snapshot is None:
            raise LookupError("A2A status request did not match a diagnosis task")
        return self.task_from_snapshot(snapshot)

    def _get_replay_task(self, envelope: A2AEnvelope) -> dict[str, Any]:
        incident_id = str(envelope.data.get("incident_id") or envelope.context_id or "")
        if not incident_id:
            raise ValueError("explain_incident_replay requires incident_id")
        replay = self._build_replay_payload(incident_id)
        task_id = new_task_id("replay")
        task = A2ATask(
            id=task_id,
            context_id=incident_id,
            status=task_status(
                task_id=task_id,
                context_id=incident_id,
                state="TASK_STATE_COMPLETED",
                text="Incident replay artifact is ready.",
            ),
            artifacts=[
                data_artifact(
                    "incident_replay",
                    "Incident Replay",
                    replay,
                    description="Replay-ready diagnosis timeline, evidence, approval, and report view.",
                )
            ],
            metadata={
                "skill": SKILL_EXPLAIN_INCIDENT_REPLAY,
                "incident_id": incident_id,
                "client_task_id": envelope.task_id or envelope.data.get("task_id", ""),
                "links": replay.get("links", {}),
            },
        )
        return dump_a2a(task)

    async def _answer_runbook_question(self, envelope: A2AEnvelope) -> dict[str, Any]:
        question = str(envelope.data.get("question") or envelope.text or "").strip()
        if not question:
            raise ValueError("answer_runbook_question requires question text")
        task_id = new_task_id("runbook")
        metadata_filter = envelope.data.get("metadata_filter")
        if not isinstance(metadata_filter, dict):
            metadata_filter = None
        payload = await self.rag_agent_service.query_with_retrieval(
            question,
            session_id=task_id,
            metadata_filter=metadata_filter,
        )
        answer = str(payload.get("answer") or "")
        task = A2ATask(
            id=task_id,
            context_id=envelope.context_id or task_id,
            status=task_status(
                task_id=task_id,
                context_id=envelope.context_id or task_id,
                state="TASK_STATE_COMPLETED",
                text=answer or "Runbook answer completed.",
            ),
            artifacts=[
                mixed_artifact(
                    "runbook_answer",
                    "Runbook Answer",
                    text=answer,
                    data=payload,
                    description="Grounded Runbook answer with citations and refusal metadata.",
                )
            ],
            metadata={
                "skill": SKILL_ANSWER_RUNBOOK_QUESTION,
                "client_task_id": envelope.task_id or envelope.data.get("task_id", ""),
                "answer_policy": payload.get("answer_policy", ""),
                "no_answer": bool(payload.get("no_answer")),
            },
        )
        return dump_a2a(task)

    def task_from_snapshot(self, snapshot: AIOpsSessionSnapshot) -> dict[str, Any]:
        """Build an A2A task from the durable run snapshot."""
        events = list_run_trace_events(snapshot, self.trace_service)
        approvals = self.approval_service.list_requests(incident_id=snapshot.incident_id)
        report = self.report_generator.get_report(snapshot.incident_id)
        run_status = build_aiops_run_status(
            snapshot,
            events=events,
            approvals=approvals,
            report=report,
        )
        return self.task_from_run_status(run_status)

    def task_from_run_status(self, run_status: dict[str, Any]) -> dict[str, Any]:
        """Build an A2A task from the existing AutoOnCall run read model."""
        task_id = str(run_status.get("session_id") or run_status.get("diagnosis_run_id") or "")
        incident_id = str(run_status.get("incident_id") or "")
        status = str(run_status.get("status") or "unknown")
        state = a2a_state_from_autooncall_status(status)
        artifacts = [
            data_artifact(
                "run_status",
                "Diagnosis Run Status",
                run_status,
                description="AutoOnCall diagnosis run status and links.",
            )
        ]
        report_payload = run_status.get("report")
        if isinstance(report_payload, dict) and report_payload:
            report_id = str(
                report_payload.get("report_id") or run_status.get("report_id") or "report"
            )
            artifacts.append(
                mixed_artifact(
                    report_id,
                    "Diagnosis Report",
                    text=str(report_payload.get("markdown") or ""),
                    data=report_payload,
                    description="Structured diagnosis report generated by AutoOnCall.",
                )
            )
        evidence = run_status.get("gathered_evidence")
        if isinstance(evidence, list) and evidence:
            artifacts.append(
                data_artifact(
                    "evidence",
                    "Evidence",
                    {"items": evidence},
                    description="Evidence normalized from diagnostic tool calls.",
                )
            )

        task = A2ATask(
            id=task_id,
            context_id=incident_id,
            status=task_status(
                task_id=task_id,
                context_id=incident_id,
                state=state,
                text=status_message(run_status),
                timestamp=run_status.get("updated_at"),
            ),
            artifacts=artifacts,
            metadata={
                "skill": SKILL_DIAGNOSE_INCIDENT,
                "incident_id": incident_id,
                "trace_id": run_status.get("trace_id", ""),
                "autooncall_status": status,
                "status_metadata": run_status.get("status_metadata", {}),
                "links": run_status.get("links", {}),
                "approval_summary": run_status.get("approval_summary", {}),
                "trace_summary": run_status.get("trace_summary", {}),
            },
        )
        return dump_a2a(task)

    def task_from_terminal_event(
        self,
        task_id: str,
        context_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a fallback A2A task when a fake/test service does not persist snapshots."""
        status = str(event.get("status") or "completed")
        state = a2a_state_from_autooncall_status(status)
        artifacts: list[A2AArtifact] = []
        report_payload = event.get("structured_report")
        if isinstance(report_payload, dict) and report_payload:
            artifacts.append(
                mixed_artifact(
                    str(report_payload.get("report_id") or "diagnosis_report"),
                    "Diagnosis Report",
                    text=str(report_payload.get("markdown") or ""),
                    data=report_payload,
                )
            )
        task = A2ATask(
            id=task_id,
            context_id=context_id or str(event.get("incident_id") or ""),
            status=task_status(
                task_id=task_id,
                context_id=context_id,
                state=state,
                text=str(event.get("message") or "Diagnosis task completed."),
            ),
            artifacts=artifacts,
            metadata={
                "skill": SKILL_DIAGNOSE_INCIDENT,
                "incident_id": event.get("incident_id", context_id),
                "trace_id": event.get("trace_id", ""),
                "autooncall_status": status,
            },
        )
        return dump_a2a(task)

    def _build_replay_payload(self, incident_id: str) -> dict[str, Any]:
        report = self.report_generator.get_report(incident_id)
        events = self.trace_service.list_events(incident_id=incident_id)
        approvals = self.approval_service.list_requests(incident_id=incident_id)
        state = self.incident_state_store.get_incident_state(incident_id)
        change_executions = [
            build_change_execution_read_model(execution)
            for execution in self.change_execution_service.list_executions(incident_id=incident_id)
        ]
        if (
            report is None
            and not events
            and not approvals
            and state is None
            and not change_executions
        ):
            raise LookupError(f"Incident not found: {incident_id}")
        return build_incident_replay(
            incident_id,
            report,
            events,
            approvals,
            state,
            change_executions,
        )


class A2AEnvelope:
    """Normalized incoming A2A message payload."""

    def __init__(
        self,
        *,
        message_id: str,
        task_id: str,
        context_id: str,
        text: str,
        data: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        self.message_id = message_id
        self.task_id = task_id
        self.context_id = context_id
        self.text = text
        self.data = data
        self.metadata = metadata


def parse_message_envelope(payload: dict[str, Any]) -> A2AEnvelope:
    """Accept A2A HTTP+JSON and simple JSON-RPC-like payloads."""
    params = payload.get("params") if isinstance(payload.get("params"), dict) else payload
    message = params.get("message") if isinstance(params.get("message"), dict) else params
    parts = message.get("parts") if isinstance(message.get("parts"), list) else []
    data = {}
    data.update(_mapping(params.get("data")))
    data.update(_data_from_parts(parts))
    metadata = {}
    metadata.update(_mapping(params.get("metadata")))
    metadata.update(_mapping(message.get("metadata")))
    metadata.update(_mapping(data.get("metadata")))
    text = str(params.get("text") or message.get("text") or _text_from_parts(parts)).strip()
    message_id = str(
        message.get("messageId")
        or message.get("message_id")
        or params.get("messageId")
        or params.get("message_id")
        or uuid4().hex
    )
    task_id = str(
        message.get("taskId")
        or message.get("task_id")
        or params.get("taskId")
        or params.get("task_id")
        or data.get("task_id")
        or data.get("session_id")
        or ""
    )
    context_id = str(
        message.get("contextId")
        or message.get("context_id")
        or params.get("contextId")
        or params.get("context_id")
        or data.get("context_id")
        or data.get("incident_id")
        or ""
    )
    return A2AEnvelope(
        message_id=message_id,
        task_id=task_id,
        context_id=context_id,
        text=text,
        data=data,
        metadata=metadata,
    )


def resolve_skill(envelope: A2AEnvelope) -> str:
    """Infer the business skill requested by the A2A caller."""
    requested = str(
        envelope.metadata.get("skill")
        or envelope.metadata.get("skill_id")
        or envelope.data.get("skill")
        or envelope.data.get("skill_id")
        or envelope.data.get("intent")
        or ""
    ).strip()
    if requested:
        return requested
    if isinstance(envelope.data.get("incident"), dict):
        return SKILL_DIAGNOSE_INCIDENT
    if envelope.data.get("replay") or envelope.data.get("include_replay"):
        return SKILL_EXPLAIN_INCIDENT_REPLAY
    if envelope.data.get("incident_id") or envelope.task_id:
        return SKILL_GET_INCIDENT_STATUS
    return SKILL_ANSWER_RUNBOOK_QUESTION


def diagnosis_task_id(envelope: A2AEnvelope) -> str:
    """Return a server-owned A2A/AutoOnCall diagnosis task id."""
    return new_task_id("diagnosis")


def new_task_id(kind: str) -> str:
    """Generate a server-owned A2A task id."""
    return f"a2a-{kind}-{uuid4().hex}"


def incident_from_envelope(envelope: A2AEnvelope) -> Incident:
    """Build the structured Incident for a diagnosis request."""
    raw_incident = envelope.data.get("incident")
    if isinstance(raw_incident, dict):
        return Incident.model_validate(raw_incident)
    if envelope.text:
        return Incident(
            title=envelope.text[:120] or "A2A incident diagnosis",
            symptom=envelope.text[:500],
            raw_alert={
                "source": "a2a",
                "message_id": envelope.message_id,
                "context_id": envelope.context_id,
            },
        )
    raise ValueError("diagnose_incident requires an incident object or text symptom")


def agent_skills(*, extended: bool = False) -> list[A2AAgentSkill]:
    """Return the business-level skills that are safe to expose over A2A."""
    examples = {
        SKILL_DIAGNOSE_INCIDENT: [
            '{"skill":"diagnose_incident","incident":{"service_name":"order-service","symptom":"Redis timeout"}}'
        ],
        SKILL_GET_INCIDENT_STATUS: ['{"skill":"get_incident_status","task_id":"a2a-..."}'],
        SKILL_EXPLAIN_INCIDENT_REPLAY: [
            '{"skill":"explain_incident_replay","incident_id":"inc-...","include_replay":true}'
        ],
        SKILL_ANSWER_RUNBOOK_QUESTION: [
            '{"skill":"answer_runbook_question","question":"Redis maxclients 告警如何排查？"}'
        ],
    }
    skills = [
        A2AAgentSkill(
            id=SKILL_DIAGNOSE_INCIDENT,
            name="Diagnose Incident",
            description=(
                "Run the AutoOnCall Plan-Execute-Replan diagnosis workflow for a "
                "structured incident. Produces evidence, trace, approval state, and report artifacts."
            ),
            tags=["aiops", "incident", "diagnosis"],
            input_modes=["application/json", "text/plain"],
            output_modes=["application/json", "text/markdown"],
            examples=examples[SKILL_DIAGNOSE_INCIDENT] if extended else [],
        ),
        A2AAgentSkill(
            id=SKILL_GET_INCIDENT_STATUS,
            name="Get Incident Status",
            description="Read the latest diagnosis task status and report links.",
            tags=["aiops", "status", "read-only"],
            input_modes=["application/json"],
            output_modes=["application/json"],
            examples=examples[SKILL_GET_INCIDENT_STATUS] if extended else [],
        ),
        A2AAgentSkill(
            id=SKILL_EXPLAIN_INCIDENT_REPLAY,
            name="Explain Incident Replay",
            description="Return replay-ready timeline, evidence quality, approval flow, and report summary.",
            tags=["aiops", "replay", "evidence", "read-only"],
            input_modes=["application/json"],
            output_modes=["application/json"],
            examples=examples[SKILL_EXPLAIN_INCIDENT_REPLAY] if extended else [],
        ),
        A2AAgentSkill(
            id=SKILL_ANSWER_RUNBOOK_QUESTION,
            name="Answer Runbook Question",
            description="Answer a Runbook question with citation and no-answer refusal safeguards.",
            tags=["rag", "runbook", "citations", "read-only"],
            input_modes=["application/json", "text/plain"],
            output_modes=["application/json", "text/markdown", "text/plain"],
            examples=examples[SKILL_ANSWER_RUNBOOK_QUESTION] if extended else [],
        ),
    ]
    return skills


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
            message_id=f"{task_id}-{state}",
            role="ROLE_AGENT",
            task_id=task_id,
            context_id=context_id,
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
        artifact_id=artifact_id,
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
        artifact_id=artifact_id,
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


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _data_from_parts(parts: list[Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("kind") or part.get("type") or "")
        if kind == "data" and isinstance(part.get("data"), dict):
            data.update(part["data"])
        elif isinstance(part.get("data"), dict) and not kind:
            data.update(part["data"])
    return data


def _text_from_parts(parts: list[Any]) -> str:
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("kind") or part.get("type") or "")
        if kind == "text" and part.get("text"):
            texts.append(str(part["text"]))
        elif not kind and part.get("text"):
            texts.append(str(part["text"]))
    return "\n".join(texts)


a2a_facade = A2AFacade()
