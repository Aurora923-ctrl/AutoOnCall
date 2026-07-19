"""请求数据模型

定义 API 请求的 Pydantic 模型
"""

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

SESSION_ID_MAX_LENGTH = 128
CHAT_QUESTION_MAX_LENGTH = 8000


class ChatRequest(BaseModel):
    """对话请求"""

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "Id": "session-123",
                "Question": "什么是向量数据库？",
            }
        },
    )

    id: str = Field(
        ...,
        min_length=1,
        max_length=SESSION_ID_MAX_LENGTH,
        description="会话 ID",
        alias="Id",
    )

    question: str = Field(
        ...,
        min_length=1,
        max_length=CHAT_QUESTION_MAX_LENGTH,
        description="用户问题",
        alias="Question",
    )

    metadata_filter: dict[str, Any] | None = Field(
        default=None,
        description="RAG 检索 metadata 精确过滤条件",
        validation_alias=AliasChoices("metadataFilter", "MetadataFilter"),
    )

    evidence_level: (
        Literal[
            "offline_fixture",
            "local_live",
            "controlled_fault",
            "production",
        ]
        | None
    ) = Field(
        default=None,
        validation_alias=AliasChoices("evidenceLevel", "EvidenceLevel"),
        description="Optional provenance level for persisted request performance evidence.",
    )

    acceptance_run_id: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("acceptanceRunId", "AcceptanceRunId"),
        description="Optional bounded run identity for local-live acceptance evidence.",
    )

    @field_validator("id", "question", mode="before")
    @classmethod
    def strip_required_text(cls, value: Any) -> Any:
        """Trim required string fields before length validation."""
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if any(ord(character) < 32 or ord(character) == 127 for character in stripped):
            raise ValueError("control characters are not allowed")
        return stripped


class ClearRequest(BaseModel):
    """清空会话请求"""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(
        ...,
        min_length=1,
        max_length=SESSION_ID_MAX_LENGTH,
        description="会话 ID",
        alias="sessionId",
    )

    @field_validator("session_id", mode="before")
    @classmethod
    def strip_session_id(cls, value: Any) -> Any:
        """Trim session IDs before length validation."""
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if any(ord(character) < 32 or ord(character) == 127 for character in stripped):
            raise ValueError("control characters are not allowed")
        return stripped
