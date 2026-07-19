"""Human feedback and bad-case models for RAG and AIOps evaluation loops."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

from app.models.incident import new_model_id, utc_now

FeedbackTarget = Literal["rag", "aiops", "change", "ragas"]
FeedbackVote = Literal["thumb_up", "thumb_down"]
FeedbackReferenceStatus = Literal["verified", "unverified", "orphaned"]
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
MAX_FEEDBACK_STRUCTURED_CHARS = 100_000
MAX_FEEDBACK_COLLECTION_ITEMS = 100
FEEDBACK_REFERENCE_METADATA_KEYS = {
    "feedback_object_id",
    "message_id",
    "incident_id",
    "report_id",
    "run_id",
    "session_id",
    "trace_id",
}


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
    idempotency_key: str = Field(default="", max_length=128)
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

    @field_validator(
        "idempotency_key",
        "reason",
        "expected_answer",
        "query",
        "answer",
        "trace_id",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        """Trim optional text fields before storing feedback."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("idempotency_key", "trace_id")
    @classmethod
    def identifiers_must_not_contain_control_characters(cls, value: str) -> str:
        """Keep client-supplied identifiers safe for audit logs and exported fixtures."""
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("feedback identifiers must not contain control characters")
        return value

    @model_validator(mode="after")
    def structured_payload_must_be_bounded(self) -> BadCaseFeedbackCreate:
        """Bound nested feedback context before it reaches JSONL persistence."""
        for field_name in ("citations", "retrieval_results", "rejected_results", "tool_calls"):
            if len(getattr(self, field_name)) > MAX_FEEDBACK_COLLECTION_ITEMS:
                raise ValueError(
                    f"{field_name} must contain at most {MAX_FEEDBACK_COLLECTION_ITEMS} items"
                )
        structured = {
            "citations": self.citations,
            "retrieval_results": self.retrieval_results,
            "rejected_results": self.rejected_results,
            "tool_calls": self.tool_calls,
            "metadata": self.metadata,
        }
        if (
            len(json.dumps(structured, ensure_ascii=False, default=str))
            > MAX_FEEDBACK_STRUCTURED_CHARS
        ):
            raise ValueError("feedback structured context is too large")
        for key in FEEDBACK_REFERENCE_METADATA_KEYS:
            value = self.metadata.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"metadata.{key} must be a string")
            if len(value) > 128:
                raise ValueError(f"metadata.{key} must contain at most 128 characters")
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError(f"metadata.{key} must not contain control characters")
        return self


class BadCaseFeedback(BaseModel):
    """Persisted feedback item used to turn bad cases into offline eval cases."""

    feedback_id: str = Field(default_factory=lambda: new_model_id("fbk"))
    owner_id: str = "anonymous"
    dedupe_fingerprint: str = ""
    incident_id: str = ""
    report_id: str = ""
    run_id: str = ""
    session_id: str = ""
    trace_id: str = ""
    target: FeedbackTarget
    vote: FeedbackVote
    category: BadCaseCategory
    category_label: str = ""
    reason: str = ""
    expected_answer: str = ""
    evidence: FeedbackEvidence = Field(default_factory=FeedbackEvidence)
    high_value: bool = False
    reference_status: FeedbackReferenceStatus = "unverified"
    orphan_reasons: list[str] = Field(default_factory=list)
    improvement_items: list[dict[str, str]] = Field(default_factory=list)
    exported_eval_case_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class EvalBacklogItem(BaseModel):
    """Reviewable draft that connects one bad case to a future eval regression case."""

    backlog_id: str = Field(default_factory=lambda: new_model_id("ebl"))
    feedback_id: str = ""
    source: str = "feedback"
    target: FeedbackTarget
    category: BadCaseCategory
    priority: EvalBacklogPriority = "P1"
    review_status: EvalBacklogReviewStatus = "new"
    suggested_eval_file: str = ""
    suggested_eval_suite: str = "aiops"
    suggested_eval_case_id: str = ""
    suggested_eval_dimension: str = ""
    expected_behavior: str = ""
    failure_reasons: list[str] = Field(default_factory=list)
    evidence_snapshot: dict[str, Any] = Field(default_factory=dict)
    links: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    reviewed_by: str = ""
    reviewed_at: datetime | None = None


class DiagnosisFeedback(BaseModel):
    """Operator feedback used to turn weak reports into improvement backlog."""

    feedback_id: str = Field(default_factory=lambda: new_model_id("fbk"))
    owner_id: str = "anonymous"
    dedupe_fingerprint: str = ""
    incident_id: str
    report_id: str = Field(max_length=128)
    run_id: str = ""
    session_id: str = ""
    trace_id: str = ""
    vote: FeedbackVote = "thumb_down"
    root_cause_correct: Literal["yes", "partial", "no"]
    accepted_suggestion: Literal["yes", "no"]
    operator_note: str = ""
    improvement_items: list[dict[str, str]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DiagnosisFeedbackCreate(BaseModel):
    """Input payload for the incident report feedback loop."""

    model_config = {"populate_by_name": True}

    report_id: str
    run_id: str = Field(default="", max_length=128)
    session_id: str = Field(default="", max_length=128)
    trace_id: str = Field(default="", max_length=128)
    root_cause_correct: Literal["yes", "partial", "no"]
    accepted_suggestion: Literal["yes", "no"]
    operator_note: str = Field(default="", max_length=2000)
    expected_answer: str = Field(default="", max_length=4000)
    category: BadCaseCategory | None = None
    vote: FeedbackVote = Field(
        default="thumb_down",
        validation_alias=AliasChoices("vote", "thumb"),
    )

    @field_validator(
        "run_id",
        "session_id",
        "trace_id",
        "operator_note",
        "expected_answer",
        mode="before",
    )
    @classmethod
    def strip_optional_text(cls, value: Any) -> Any:
        """Trim optional report-feedback strings."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("report_id", "run_id", "session_id", "trace_id")
    @classmethod
    def identifiers_must_not_contain_control_characters(cls, value: str) -> str:
        """Keep runtime links safe for audit logs and exported fixtures."""
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("feedback identifiers must not contain control characters")
        return value
