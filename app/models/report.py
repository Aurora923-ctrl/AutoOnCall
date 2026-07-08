"""Diagnosis report models for AIOps incidents."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.incident import new_model_id, utc_now


class DiagnosisReport(BaseModel):
    """Structured diagnosis and post-incident report."""

    report_id: str = Field(default_factory=lambda: new_model_id("rpt"))
    incident_id: str
    trace_id: str = ""
    title: str = "AIOps diagnosis report"
    service_name: str = "unknown-service"
    severity: str = "P2"
    environment: str = "unknown"
    status: str = "completed"
    summary: str = ""
    root_cause: str = ""
    hypotheses: list[str] = Field(default_factory=list)
    hypothesis_ranking: list[dict[str, Any]] = Field(default_factory=list)
    selected_root_cause_id: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    confirmed_facts: list[str] = Field(default_factory=list)
    inferred_conclusions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    dependency_signals: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    impact: str = ""
    risk_summary: dict[str, Any] = Field(default_factory=dict)
    manual_action_required: bool = False
    approval_status: str = "not_required"
    approval_decision: dict[str, Any] = Field(default_factory=dict)
    change_plan: dict[str, Any] = Field(default_factory=dict)
    change_executions: list[dict[str, Any]] = Field(default_factory=list)
    remediation_suggestion: str = ""
    prevention: str = ""
    trace_summary: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evidence_profile: dict[str, Any] = Field(default_factory=dict)
    evidence_sufficiency: dict[str, Any] = Field(default_factory=dict)
    evidence_graph: dict[str, Any] = Field(default_factory=dict)
    conclusion_alignment: dict[str, Any] = Field(default_factory=dict)
    confidence_reason: str = ""
    uncertainties: list[str] = Field(default_factory=list)
    markdown: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=utc_now)
