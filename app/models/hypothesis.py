"""Root-cause hypothesis models for explainable AIOps diagnosis."""

from typing import Any

from pydantic import BaseModel, Field

from app.models.incident import new_model_id


class RootCauseHypothesis(BaseModel):
    """Ranked root-cause hypothesis with evidence attribution."""

    hypothesis_id: str = Field(default_factory=lambda: new_model_id("hyp"))
    title: str
    description: str = ""
    category: str = "unknown"
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    refuting_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence_reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def evidence_ids(self) -> list[str]:
        """Return stable supporting evidence IDs for RCA evaluation and APIs."""
        return list(self.supporting_evidence_ids)
