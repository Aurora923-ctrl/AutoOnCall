"""Evidence models collected during AIOps diagnosis."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.incident import new_model_id, utc_now
from app.services.diagnostic_signal_rules import (
    build_confidence_reason as _build_confidence_reason,
    infer_evidence_stance as _infer_evidence_stance,
    infer_evidence_type as _infer_evidence_type,
    normalize_data_source as _normalize_data_source,
)

EvidenceType = Literal[
    "metric",
    "log",
    "runbook",
    "k8s",
    "redis",
    "mysql",
    "ticket",
    "alert",
    "trace",
    "message_queue",
    "service_context",
    "change",
    "risk",
    "unknown",
]
EvidenceStance = Literal["supporting", "refuting", "neutral", "unknown"]
EvidenceSource = Literal[
    "prometheus",
    "loki",
    "log_gateway",
    "cmdb",
    "deploy_history",
    "redis_info",
    "kubernetes",
    "mysql",
    "ticket_api",
    "alertmanager",
    "jaeger",
    "tempo",
    "redpanda",
    "mcp_monitor",
    "mcp_monitor_mixed",
    "mcp_cls",
    "mock",
    "rule_based",
    "not_configured",
    "failed",
    "manual_analysis",
    "llm_toolnode_fallback",
    "rag",
    "unknown",
]


class Evidence(BaseModel):
    """Structured evidence produced by a tool call or analysis step."""

    evidence_id: str = Field(default_factory=lambda: new_model_id("evd"))
    source_tool: str
    step_id: str
    summary: str
    evidence_type: EvidenceType = "unknown"
    data_source: EvidenceSource = "unknown"
    stance: EvidenceStance = "neutral"
    confidence_reason: str = ""
    fact: str = ""
    inference: str = ""
    uncertainty: str = ""
    next_step: str = ""
    raw_data: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    related_hypothesis: str = ""
    created_at: datetime = Field(default_factory=utc_now)


def normalize_data_source(
    source_tool: str, raw_data: dict[str, Any] | None = None
) -> EvidenceSource:
    """Return a stable provenance label for reports, traces, and UI badges."""
    return _normalize_data_source(source_tool, raw_data)  # type: ignore[return-value]


def infer_evidence_type(source_tool: str) -> EvidenceType:
    """Infer a stable evidence domain from the producing tool name."""
    return _infer_evidence_type(source_tool)  # type: ignore[return-value]


def infer_evidence_stance(
    *,
    source_tool: str,
    raw_data: dict[str, Any],
    summary: str = "",
) -> EvidenceStance:
    """Infer whether evidence supports, refutes, neutrally describes, or cannot judge."""
    return _infer_evidence_stance(
        source_tool=source_tool,
        raw_data=raw_data,
        summary=summary,
    )  # type: ignore[return-value]


def build_confidence_reason(
    *,
    source_tool: str,
    raw_data: dict[str, Any],
    stance: EvidenceStance,
) -> str:
    """Build a short explanation for an evidence confidence score."""
    return _build_confidence_reason(
        source_tool=source_tool,
        raw_data=raw_data,
        stance=stance,
    )
