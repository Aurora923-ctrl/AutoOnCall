"""A2A skill constants and agent-card skill descriptors."""

from __future__ import annotations

from app.models.a2a import A2AAgentSkill

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
            '{"skill":"answer_runbook_question","question":"How should Redis maxclients alerts be investigated?"}'
        ],
    }
    return [
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
