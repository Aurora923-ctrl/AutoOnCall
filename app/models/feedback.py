"""Human feedback and bad-case models for RAG and AIOps evaluation loops."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator

from app.models.incident import new_model_id, utc_now

FeedbackTarget = Literal["rag", "aiops", "change", "ragas"]
FeedbackVote = Literal["thumb_up", "thumb_down"]
EvalBacklogPriority = Literal["P0", "P1", "P2"]
EvalBacklogReviewStatus = Literal["new", "reviewed", "promoted", "rejected"]
BadCaseCategory = Literal[
    "retrieval_failure",
    "missing_citation",
    "tool_failure",
    "hallucination_risk",
    "permission_denied",
    "poor_report_quality",
]


BAD_CASE_CATEGORY_LABELS: dict[BadCaseCategory, str] = {
    "retrieval_failure": "召回失败",
    "missing_citation": "引用缺失",
    "tool_failure": "工具失败",
    "hallucination_risk": "幻觉风险",
    "permission_denied": "权限拒绝",
    "poor_report_quality": "报告质量差",
}


class FeedbackEvidence(BaseModel):
    """Runtime context captured with a feedback item for later eval-case drafting."""

    query: str = ""
    answer: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_results: list[dict[str, Any]] = Field(default_factory=list)
    rejected_results: list[dict[str, Any]] = Field(default_factory=list)
    trace_id: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BadCaseFeedbackCreate(BaseModel):
    """Input payload for RAG/AIOps thumb feedback."""

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {
                "target": "rag",
                "vote": "thumb_down",
                "reason": "召回到了 CPU 文档，但问题实际是 Redis maxclients。",
                "expected_answer": "应引用 redis_maxclients.md 并说明 connected_clients 接近上限。",
                "category": "retrieval_failure",
                "query": "order-service Redis timeout 怎么排查？",
                "answer": "当前回答内容",
                "citations": [],
                "retrieval_results": [],
                "rejected_results": [],
                "trace_id": "trace-1",
                "tool_calls": [],
                "metadata": {"session_id": "session-1"},
            }
        },
    }

    target: FeedbackTarget
    vote: FeedbackVote
    reason: str = Field(default="", max_length=2000)
    expected_answer: str = Field(default="", max_length=4000)
    category: BadCaseCategory | None = None
    query: str = Field(default="", max_length=8000)
    answer: str = Field(default="", max_length=12000)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_results: list[dict[str, Any]] = Field(default_factory=list)
    rejected_results: list[dict[str, Any]] = Field(default_factory=list)
    trace_id: str = Field(default="", max_length=128)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("reason", "expected_answer", "query", "answer", "trace_id", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        """Trim optional text fields before storing feedback."""
        return value.strip() if isinstance(value, str) else value


class BadCaseFeedback(BaseModel):
    """Persisted feedback item used to turn bad cases into offline eval cases."""

    feedback_id: str = Field(default_factory=lambda: new_model_id("fbk"))
    target: FeedbackTarget
    vote: FeedbackVote
    category: BadCaseCategory
    category_label: str = ""
    reason: str = ""
    expected_answer: str = ""
    evidence: FeedbackEvidence = Field(default_factory=FeedbackEvidence)
    high_value: bool = False
    improvement_items: list[dict[str, str]] = Field(default_factory=list)
    exported_eval_case_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class EvalBacklogItem(BaseModel):
    """Reviewable draft that connects one bad case to a future eval regression case."""

    backlog_id: str = Field(default_factory=lambda: new_model_id("ebl"))
    feedback_id: str
    source: str = "feedback"
    target: FeedbackTarget
    category: BadCaseCategory
    priority: EvalBacklogPriority = "P1"
    review_status: EvalBacklogReviewStatus = "new"
    suggested_eval_file: str
    suggested_eval_suite: str = "aiops"
    suggested_eval_case_id: str
    suggested_eval_dimension: str
    expected_behavior: str = ""
    failure_reasons: list[str] = Field(default_factory=list)
    evidence_snapshot: dict[str, Any] = Field(default_factory=dict)
    links: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class DiagnosisFeedback(BaseModel):
    """Operator feedback used to turn weak reports into improvement backlog."""

    feedback_id: str = Field(default_factory=lambda: new_model_id("fbk"))
    incident_id: str
    report_id: str
    root_cause_correct: Literal["yes", "partial", "no"]
    accepted_suggestion: Literal["yes", "no"]
    operator_note: str = ""
    improvement_items: list[dict[str, str]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class DiagnosisFeedbackCreate(BaseModel):
    """Input payload for the incident report feedback loop."""

    model_config = {"populate_by_name": True}

    report_id: str
    root_cause_correct: Literal["yes", "partial", "no"]
    accepted_suggestion: Literal["yes", "no"]
    operator_note: str = Field(default="", max_length=2000)
    expected_answer: str = Field(default="", max_length=4000)
    category: BadCaseCategory | None = None
    vote: FeedbackVote = Field(
        default="thumb_down",
        validation_alias=AliasChoices("vote", "thumb"),
    )

    @field_validator("operator_note", "expected_answer", mode="before")
    @classmethod
    def strip_optional_text(cls, value: Any) -> Any:
        """Trim optional report-feedback strings."""
        return value.strip() if isinstance(value, str) else value
