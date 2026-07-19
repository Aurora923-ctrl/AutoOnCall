"""Service orchestration for exposing AutoOnCall through A2A."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

from app.config import config
from app.models.a2a import (
    A2A_PROTOCOL_VERSION,
    A2AAgentCard,
    A2AArtifact,
    A2ATask,
    A2ATaskRecord,
    A2ATaskState,
)
from app.models.aiops_session import AIOpsSessionSnapshot
from app.services.a2a_messages import (
    A2AEnvelope,
    diagnosis_task_id,
    incident_from_envelope,
    parse_message_envelope,
    resolve_skill,
    task_id_for_envelope,
)
from app.services.a2a_payloads import (
    a2a_state_from_autooncall_status,
    data_artifact,
    diagnosis_event_to_a2a_event,
    dump_a2a,
    mixed_artifact,
    status_message,
    status_update_event,
    task_status,
)
from app.services.a2a_skills import (
    SKILL_ANSWER_RUNBOOK_QUESTION,
    SKILL_DIAGNOSE_INCIDENT,
    SKILL_EXPLAIN_INCIDENT_REPLAY,
    SKILL_GET_INCIDENT_STATUS,
    agent_skills,
)
from app.services.aiops_read_models import build_incident_replay
from app.services.aiops_read_models.common import (
    list_run_trace_events,
    select_run_approvals,
    select_run_report,
)
from app.services.aiops_read_models.run import build_aiops_run_status
from app.services.aiops_service import aiops_service as default_aiops_service
from app.services.aiops_store import create_aiops_store
from app.services.approval_service import approval_service as default_approval_service
from app.services.change_execution_read_models import build_change_execution_read_model
from app.services.change_execution_service import (
    change_execution_service as default_change_execution_service,
)
from app.services.rag_agent_service import rag_agent_service as default_rag_agent_service
from app.services.report_generator import report_generator as default_report_generator
from app.services.trace_service import trace_service as default_trace_service
from app.utils.public_errors import public_exception_message


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
        task_store: Any | None = None,
    ) -> None:
        self.aiops_service = aiops_service or default_aiops_service
        self.trace_service = trace_service or default_trace_service
        self.approval_service = approval_service or default_approval_service
        self.report_generator = report_generator or default_report_generator
        self.change_execution_service = change_execution_service or default_change_execution_service
        self.rag_agent_service = rag_agent_service or default_rag_agent_service
        self.incident_state_store = incident_state_store or create_aiops_store()
        self.task_store = task_store or self.incident_state_store

    def agent_card(self, *, extended: bool = False) -> dict[str, Any]:
        """Return the A2A Agent Card for capability discovery."""
        base_url = config.normalized_api_base_url
        base_path = config.normalized_a2a_base_path
        agent_url = f"{base_url}{base_path}"
        security_schemes: dict[str, Any] = {
            "bearerAuth": {
                "httpAuthSecurityScheme": {
                    "scheme": "Bearer",
                    "description": "AutoOnCall API token with diagnose/read scope.",
                }
            }
        }
        security_requirements: list[dict[str, list[str]]] = [{"bearerAuth": []}]

        card = A2AAgentCard(
            name=config.a2a_agent_name,
            description=(
                "A2A-compatible OnCall diagnosis agent for incident investigation, "
                "evidence replay, and cited Runbook answers. It does not expose "
                "low-level infrastructure tools or production change execution."
            ),
            supportedInterfaces=[
                {
                    "protocolBinding": "HTTP+JSON",
                    "url": agent_url,
                    "protocolVersion": A2A_PROTOCOL_VERSION,
                },
            ],
            provider={
                "organization": "AutoOnCall",
                "url": config.normalized_api_base_url,
            },
            version=config.app_version,
            documentationUrl=f"{base_url}/docs",
            capabilities={
                "streaming": True,
                "pushNotifications": False,
                "extendedAgentCard": True,
                "extensions": [
                    {"uri": "urn:autooncall:a2a:incident-replay", "required": False},
                    {"uri": "urn:autooncall:a2a:evidence-artifacts", "required": False},
                ],
            },
            securitySchemes=security_schemes,
            securityRequirements=security_requirements,
            defaultInputModes=["text/plain", "application/json"],
            defaultOutputModes=["application/json", "text/markdown", "text/plain"],
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
            yield {"task": response["task"]}
            return

        task_id = diagnosis_task_id(envelope)
        incident = incident_from_envelope(envelope)
        existing_record = self._get_task_record(task_id)
        if existing_record is not None:
            self._ensure_record_matches(existing_record, envelope)
            existing_snapshot = self.aiops_service.get_session_snapshot(task_id)
            if existing_snapshot is not None:
                existing_task = self.task_from_snapshot(existing_snapshot)
                self._save_task_record(existing_task, envelope)
            else:
                existing_task = self._task_from_record(existing_record)
            yield {"task": existing_task}
            return
        context_id = envelope.context_id or incident.incident_id
        if not self._create_task_record(
            task_id=task_id,
            skill=skill,
            incident_id=incident.incident_id,
            state="TASK_STATE_SUBMITTED",
            envelope=envelope,
        ):
            existing_record = self._get_task_record(task_id)
            if existing_record is None:
                raise RuntimeError("A2A task persistence conflict")
            self._ensure_record_matches(existing_record, envelope)
            existing_task = self._task_from_record(existing_record)
            if existing_task is not None:
                yield {"task": existing_task}
                return
            raise LookupError("A2A diagnosis task is already in progress")
        yield status_update_event(
            task_id=task_id,
            context_id=context_id,
            state="TASK_STATE_SUBMITTED",
            message="AutoOnCall diagnosis task accepted.",
            final=False,
            initial=True,
        )

        last_event: dict[str, Any] = {}
        try:
            async for event in self.aiops_service.diagnose(session_id=task_id, incident=incident):
                last_event = dict(event or {})
                converted = diagnosis_event_to_a2a_event(
                    task_id=task_id,
                    context_id=context_id,
                    event=last_event,
                )
                if converted and not _event_is_terminal(last_event):
                    yield converted

            snapshot = self.aiops_service.get_session_snapshot(task_id)
            task = (
                self.task_from_snapshot(snapshot)
                if snapshot is not None
                else self.task_from_terminal_event(task_id, context_id, last_event)
            )
            self._save_task_record(task, envelope)
            yield {"task": task}
        except Exception as exc:
            self._save_failed_task_record(
                task_id=task_id,
                context_id=context_id,
                envelope=envelope,
                message=public_exception_message(exc),
            )
            raise

    def get_task(self, task_id: str, *, owner_id: str = "") -> dict[str, Any]:
        """Return one A2A task from the durable AutoOnCall session snapshot."""
        if not self._is_a2a_task_id(task_id):
            raise LookupError(f"A2A task not found: {task_id}")
        record = self._get_task_record(task_id)
        if record is None:
            raise LookupError(f"A2A task not found: {task_id}")
        self._ensure_record_owner(record, owner_id)
        if record.skill != SKILL_DIAGNOSE_INCIDENT:
            return self._task_from_record(record)
        snapshot = self.aiops_service.get_session_snapshot(task_id)
        if snapshot is None:
            return self._task_from_record(record)
        if not self._is_a2a_diagnosis_snapshot(snapshot):
            raise LookupError(f"A2A task not found: {task_id}")
        task = self.task_from_snapshot(snapshot)
        self._save_task_record(task, self._envelope_from_record(record))
        return task

    def list_tasks(
        self,
        *,
        incident_id: str | None = None,
        limit: int = 20,
        owner_id: str = "",
    ) -> dict[str, Any]:
        """Return recent A2A task views backed by diagnosis runs."""
        records = self.task_store.list_a2a_task_records(
            incident_id=incident_id,
            limit=limit,
            owner_id=owner_id,
        )
        items: list[dict[str, Any]] = []
        for record in records:
            task = None
            if record.skill == SKILL_DIAGNOSE_INCIDENT:
                snapshot = self.aiops_service.get_session_snapshot(record.task_id)
                if snapshot is not None:
                    task = self.task_from_snapshot(snapshot)
            items.append(task or self._task_from_record(record))
        return {
            "items": items,
            "count": len(items),
        }

    async def _send_diagnosis(self, envelope: A2AEnvelope) -> dict[str, Any]:
        task_id = diagnosis_task_id(envelope)
        incident = incident_from_envelope(envelope)
        existing_record = self._get_task_record(task_id)
        if existing_record is not None:
            self._ensure_record_matches(existing_record, envelope)
            existing_snapshot = self.aiops_service.get_session_snapshot(task_id)
            if existing_snapshot is not None:
                existing_task = self.task_from_snapshot(existing_snapshot)
                self._save_task_record(existing_task, envelope)
                return existing_task
            return self._task_from_record(existing_record)
        if not self._create_task_record(
            task_id=task_id,
            skill=SKILL_DIAGNOSE_INCIDENT,
            incident_id=incident.incident_id,
            state="TASK_STATE_SUBMITTED",
            envelope=envelope,
        ):
            existing_record = self._get_task_record(task_id)
            if existing_record is None:
                raise RuntimeError("A2A task persistence conflict")
            self._ensure_record_matches(existing_record, envelope)
            existing_task = self._task_from_record(existing_record)
            if existing_task is not None:
                return existing_task
            raise LookupError("A2A diagnosis task is already in progress")
        last_event: dict[str, Any] = {}
        try:
            async for event in self.aiops_service.diagnose(session_id=task_id, incident=incident):
                last_event = dict(event or {})

            snapshot = self.aiops_service.get_session_snapshot(task_id)
            if snapshot is not None:
                task = self.task_from_snapshot(snapshot)
            else:
                task = self.task_from_terminal_event(
                    task_id,
                    envelope.context_id or incident.incident_id,
                    last_event,
                )
            self._save_task_record(task, envelope)
            return task
        except Exception as exc:
            self._save_failed_task_record(
                task_id=task_id,
                context_id=envelope.context_id or incident.incident_id,
                envelope=envelope,
                message=public_exception_message(exc),
            )
            raise

    def _get_status_task(self, envelope: A2AEnvelope) -> dict[str, Any]:
        task_id = (
            str(envelope.data.get("task_id") or envelope.data.get("session_id") or "")
            or envelope.task_id
        )
        incident_id = str(envelope.data.get("incident_id") or envelope.context_id or "")
        snapshot = None
        if task_id:
            if not self._is_a2a_task_id(task_id):
                raise LookupError("A2A status request did not match a diagnosis task")
            record = self._get_task_record(task_id)
            if record is None or record.skill != SKILL_DIAGNOSE_INCIDENT:
                raise LookupError("A2A status request did not match a diagnosis task")
            self._ensure_record_owner(record, _owner_id_from_envelope(envelope))
            snapshot = self.aiops_service.get_session_snapshot(task_id)
        elif incident_id:
            records = self.task_store.list_a2a_task_records(
                incident_id=incident_id,
                limit=100,
                owner_id=_owner_id_from_envelope(envelope),
            )
            record = next(
                (item for item in records if item.skill == SKILL_DIAGNOSE_INCIDENT),
                None,
            )
            snapshot = (
                self.aiops_service.get_session_snapshot(record.task_id)
                if record is not None
                else None
            )
        if snapshot is None:
            raise LookupError("A2A status request did not match a diagnosis task")
        task = self.task_from_snapshot(snapshot)
        if record is not None:
            self._save_task_record(task, self._envelope_from_record(record))
        return task

    def _get_replay_task(self, envelope: A2AEnvelope) -> dict[str, Any]:
        incident_id = str(envelope.data.get("incident_id") or envelope.context_id or "")
        if not incident_id:
            raise ValueError("explain_incident_replay requires incident_id")
        task_id = task_id_for_envelope("replay", envelope)
        existing_record = self._get_task_record(task_id)
        if existing_record is not None:
            self._ensure_record_matches(existing_record, envelope)
            return self._task_from_record(existing_record)
        if not self._create_task_record(
            task_id=task_id,
            skill=SKILL_EXPLAIN_INCIDENT_REPLAY,
            incident_id=incident_id,
            state="TASK_STATE_SUBMITTED",
            envelope=envelope,
        ):
            existing_record = self._get_task_record(task_id)
            if existing_record is None:
                raise RuntimeError("A2A task persistence conflict")
            self._ensure_record_matches(existing_record, envelope)
            return self._task_from_record(existing_record)
        try:
            replay = self._build_replay_payload(incident_id)
            task = A2ATask(
                id=task_id,
                contextId=incident_id,
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
                        description=(
                            "Replay-ready diagnosis timeline, evidence, approval, and report view."
                        ),
                    )
                ],
                metadata={
                    "skill": SKILL_EXPLAIN_INCIDENT_REPLAY,
                    "incident_id": incident_id,
                    "client_task_id": envelope.task_id or envelope.data.get("task_id", ""),
                    "request_fingerprint": envelope.request_fingerprint,
                    "links": replay.get("links", {}),
                },
            )
            task_payload = dict(dump_a2a(task))
            self._save_task_record(task_payload, envelope)
            return task_payload
        except Exception as exc:
            self._save_failed_task_record(
                task_id=task_id,
                context_id=incident_id,
                skill=SKILL_EXPLAIN_INCIDENT_REPLAY,
                envelope=envelope,
                message=public_exception_message(exc),
            )
            raise

    async def _answer_runbook_question(self, envelope: A2AEnvelope) -> dict[str, Any]:
        question = str(envelope.data.get("question") or envelope.text or "").strip()
        if not question:
            raise ValueError("answer_runbook_question requires question text")
        task_id = task_id_for_envelope("runbook", envelope)
        existing_record = self._get_task_record(task_id)
        if existing_record is not None:
            self._ensure_record_matches(existing_record, envelope)
            return self._task_from_record(existing_record)
        context_id = envelope.context_id or task_id
        if not self._create_task_record(
            task_id=task_id,
            skill=SKILL_ANSWER_RUNBOOK_QUESTION,
            incident_id=context_id,
            state="TASK_STATE_SUBMITTED",
            envelope=envelope,
        ):
            existing_record = self._get_task_record(task_id)
            if existing_record is None:
                raise RuntimeError("A2A task persistence conflict")
            self._ensure_record_matches(existing_record, envelope)
            return self._task_from_record(existing_record)
        metadata_filter = envelope.data.get("metadata_filter")
        if not isinstance(metadata_filter, dict):
            metadata_filter = None
        try:
            payload = await self.rag_agent_service.query_with_retrieval(
                question,
                session_id=task_id,
                metadata_filter=metadata_filter,
            )
            answer = str(payload.get("answer") or "")
            task = A2ATask(
                id=task_id,
                contextId=context_id,
                status=task_status(
                    task_id=task_id,
                    context_id=context_id,
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
                    "request_fingerprint": envelope.request_fingerprint,
                    "answer_policy": payload.get("answer_policy", ""),
                    "no_answer": bool(payload.get("no_answer")),
                },
            )
            task_payload = dict(dump_a2a(task))
            self._save_task_record(task_payload, envelope)
            return task_payload
        except Exception as exc:
            self._save_failed_task_record(
                task_id=task_id,
                context_id=context_id,
                skill=SKILL_ANSWER_RUNBOOK_QUESTION,
                envelope=envelope,
                message=public_exception_message(exc),
            )
            raise

    def task_from_snapshot(self, snapshot: AIOpsSessionSnapshot) -> dict[str, Any]:
        """Build an A2A task from the durable run snapshot."""
        events = list_run_trace_events(snapshot, self.trace_service)
        approvals = select_run_approvals(
            snapshot,
            self.approval_service.list_requests(incident_id=snapshot.incident_id),
        )
        report = select_run_report(
            snapshot,
            self.report_generator.get_report(snapshot.incident_id),
        )
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
        run_status_payload = _a2a_run_status_payload(run_status)
        artifacts = [
            data_artifact(
                "run_status",
                "Diagnosis Run Status",
                run_status_payload,
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
            contextId=incident_id,
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
        return dict(dump_a2a(task))

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
            contextId=context_id or str(event.get("incident_id") or ""),
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
        return dict(dump_a2a(task))

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

    @staticmethod
    def _is_a2a_task_id(task_id: str) -> bool:
        return task_id.startswith(("a2a-diagnosis-", "a2a-replay-", "a2a-runbook-"))

    @staticmethod
    def _is_a2a_diagnosis_snapshot(snapshot: AIOpsSessionSnapshot) -> bool:
        return snapshot.session_id.startswith("a2a-diagnosis-")

    def _get_task_record(self, task_id: str) -> A2ATaskRecord | None:
        return self.task_store.get_a2a_task_record(task_id)

    def _create_task_record(
        self,
        *,
        task_id: str,
        skill: str,
        incident_id: str,
        state: A2ATaskState,
        envelope: A2AEnvelope,
    ) -> bool:
        return self.task_store.create_a2a_task_record(
            A2ATaskRecord(
                task_id=task_id,
                message_id=envelope.message_id,
                request_fingerprint=envelope.request_fingerprint,
                owner_id=_owner_id_from_envelope(envelope),
                skill=skill,
                incident_id=incident_id,
                state=state,
            )
        )

    def _save_task_record(self, task: dict[str, Any], envelope: A2AEnvelope) -> None:
        task_id = str(task.get("id") or "")
        context_id = str(task.get("contextId") or task_id)
        state = cast(
            A2ATaskState,
            str((task.get("status") or {}).get("state") or "TASK_STATE_UNSPECIFIED"),
        )
        skill = str((task.get("metadata") or {}).get("skill") or "")
        existing = self._get_task_record(task_id)
        if existing is not None:
            self._ensure_record_matches(existing, envelope)
        self.task_store.save_a2a_task_record(
            A2ATaskRecord(
                task_id=task_id,
                message_id=envelope.message_id,
                request_fingerprint=envelope.request_fingerprint,
                owner_id=existing.owner_id
                if existing is not None
                else _owner_id_from_envelope(envelope),
                skill=skill,
                incident_id=context_id,
                state=state,
                task=task,
            )
        )

    def _save_failed_task_record(
        self,
        *,
        task_id: str,
        context_id: str,
        skill: str = SKILL_DIAGNOSE_INCIDENT,
        envelope: A2AEnvelope,
        message: str,
    ) -> None:
        task = A2ATask(
            id=task_id,
            contextId=context_id,
            status=task_status(
                task_id=task_id,
                context_id=context_id,
                state="TASK_STATE_FAILED",
                text=message,
            ),
            metadata={
                "skill": skill,
                "incident_id": context_id,
                "autooncall_status": "failed",
            },
        )
        self._save_task_record(dict(dump_a2a(task)), envelope)

    @staticmethod
    def _task_from_record(record: A2ATaskRecord) -> dict[str, Any]:
        if record.task:
            return dict(record.task)
        task = A2ATask(
            id=record.task_id,
            contextId=record.incident_id or record.task_id,
            status=task_status(
                task_id=record.task_id,
                context_id=record.incident_id or record.task_id,
                state=record.state,
                text="A2A task accepted and processing.",
            ),
            metadata={
                "skill": record.skill,
                "incident_id": record.incident_id,
            },
        )
        return dict(dump_a2a(task))

    @staticmethod
    def _ensure_record_matches(record: A2ATaskRecord, envelope: A2AEnvelope) -> None:
        if record.message_id != envelope.message_id:
            raise ValueError("A2A task does not match the caller message")
        if record.request_fingerprint != envelope.request_fingerprint:
            raise ValueError("A2A messageId was reused with a different request")

    @staticmethod
    def _ensure_record_owner(record: A2ATaskRecord, owner_id: str) -> None:
        if owner_id and record.owner_id != owner_id:
            raise LookupError("A2A task not found")

    @staticmethod
    def _envelope_from_record(record: A2ATaskRecord) -> A2AEnvelope:
        return A2AEnvelope(
            message_id=record.message_id,
            task_id=record.task_id,
            context_id=record.incident_id,
            text="",
            data={},
            metadata={},
            request_fingerprint=record.request_fingerprint,
        )


def _event_is_terminal(event: dict[str, Any]) -> bool:
    return str(event.get("type") or "") in {"complete", "error"}


def _owner_id_from_envelope(envelope: A2AEnvelope) -> str:
    message_id = envelope.message_id
    if not message_id.startswith("principal:"):
        return ""
    _, owner_id, _ = message_id.split(":", 2)
    return owner_id


def _a2a_run_status_payload(run_status: dict[str, Any]) -> dict[str, Any]:
    """Project the internal run model onto the stable A2A status boundary."""
    incident = run_status.get("incident")
    incident_payload = dict(incident) if isinstance(incident, dict) else {}
    return {
        "diagnosis_run_id": run_status.get("diagnosis_run_id")
        or run_status.get("session_id")
        or "",
        "incident_id": run_status.get("incident_id", ""),
        "trace_id": run_status.get("trace_id", ""),
        "status": run_status.get("status", "unknown"),
        "status_metadata": run_status.get("status_metadata", {}),
        "started_at": run_status.get("started_at", ""),
        "updated_at": run_status.get("updated_at", ""),
        "incident": {
            key: incident_payload.get(key)
            for key in (
                "incident_id",
                "title",
                "service_name",
                "severity",
                "symptom",
                "start_time",
                "environment",
                "status",
            )
            if incident_payload.get(key) is not None
        },
        "progress": run_status.get("progress", {}),
        "has_report": bool(run_status.get("has_report")),
        "report_id": run_status.get("report_id"),
        "final_diagnosis": run_status.get("final_diagnosis"),
        "remediation_suggestion": run_status.get("remediation_suggestion"),
        "errors": run_status.get("errors", []),
        "warnings": run_status.get("warnings", []),
        "trace_summary": run_status.get("trace_summary", {}),
        "approval_summary": run_status.get("approval_summary", {}),
        "links": run_status.get("links", {}),
    }
