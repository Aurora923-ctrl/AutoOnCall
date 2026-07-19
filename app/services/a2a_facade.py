"""Compatibility entrypoint for the AutoOnCall A2A facade."""

from __future__ import annotations

from app.services.a2a_facade_core import A2AFacade
from app.services.a2a_messages import (
    A2AEnvelope,
    diagnosis_task_id,
    incident_from_envelope,
    parse_message_envelope,
    resolve_skill,
    scope_message_to_principal,
    task_id_for_envelope,
)
from app.services.a2a_payloads import (
    a2a_state_from_autooncall_status,
    data_artifact,
    data_part,
    diagnosis_event_to_a2a_event,
    dump_a2a,
    mixed_artifact,
    normalized_a2a_base_path,
    status_message,
    status_update_event,
    task_status,
    text_part,
)
from app.services.a2a_skills import (
    READ_ONLY_SKILLS,
    SKILL_ANSWER_RUNBOOK_QUESTION,
    SKILL_DIAGNOSE_INCIDENT,
    SKILL_EXPLAIN_INCIDENT_REPLAY,
    SKILL_GET_INCIDENT_STATUS,
    SUPPORTED_A2A_SKILLS,
    agent_skills,
)

__all__ = [
    "A2AEnvelope",
    "A2AFacade",
    "READ_ONLY_SKILLS",
    "SKILL_ANSWER_RUNBOOK_QUESTION",
    "SKILL_DIAGNOSE_INCIDENT",
    "SKILL_EXPLAIN_INCIDENT_REPLAY",
    "SKILL_GET_INCIDENT_STATUS",
    "SUPPORTED_A2A_SKILLS",
    "a2a_facade",
    "a2a_state_from_autooncall_status",
    "agent_skills",
    "data_artifact",
    "data_part",
    "diagnosis_event_to_a2a_event",
    "diagnosis_task_id",
    "dump_a2a",
    "incident_from_envelope",
    "mixed_artifact",
    "normalized_a2a_base_path",
    "parse_message_envelope",
    "resolve_skill",
    "scope_message_to_principal",
    "status_message",
    "status_update_event",
    "task_status",
    "task_id_for_envelope",
    "text_part",
]

a2a_facade = A2AFacade()
