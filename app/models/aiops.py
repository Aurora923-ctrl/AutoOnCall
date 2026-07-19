"""AIOps API request and compatibility response models."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.incident import Incident

AIOPS_SESSION_ID_MAX_LENGTH = 128
AIOPS_APPROVAL_ID_MAX_LENGTH = 128


class AIOpsRequest(BaseModel):
    """Request body for the streaming `/api/aiops` diagnosis endpoint."""

    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=AIOPS_SESSION_ID_MAX_LENGTH,
        description="会话ID，用于追踪诊断历史",
    )

    incident: Incident | None = Field(
        default=None,
        description="结构化故障事件；不传时后端会根据当前任务自动构造默认 Incident",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "session_id": "session-123",
                "incident": {
                    "title": "order-service Redis timeout",
                    "service_name": "order-service",
                    "severity": "P1",
                    "symptom": "5xx 错误率升高，P95 延迟超过 3 秒，并出现 Redis connection timeout",
                    "environment": "prod",
                },
            }
        }
    )

    @field_validator("session_id", mode="before")
    @classmethod
    def strip_session_id(cls, value: Any) -> Any:
        """Trim optional session IDs before length validation."""
        return _strip_identifier(value)


class AIOpsResumeRequest(BaseModel):
    """Request body for resuming a paused AIOps diagnosis after approval."""

    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=AIOPS_SESSION_ID_MAX_LENGTH,
        description="原诊断会话 ID；新审批会从 metadata 自动推断，旧审批可显式传入。",
    )
    approval_id: str = Field(
        min_length=1,
        max_length=AIOPS_APPROVAL_ID_MAX_LENGTH,
        description="要恢复的审批 ID。",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "session_id": "session-123",
                "approval_id": "apr_xxx",
            }
        }
    )

    @field_validator("session_id", "approval_id", mode="before")
    @classmethod
    def strip_optional_ids(cls, value: Any) -> Any:
        """Trim optional IDs before length validation."""
        return _strip_identifier(value)


def _strip_identifier(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in stripped):
        raise ValueError("control characters are not allowed")
    return stripped


class AlertInfo(BaseModel):
    """Legacy alert shape kept for older non-streaming callers."""

    alertname: str
    severity: str
    instance: str
    duration: str
    description: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "deprecated": True,
            "description": "Legacy alert shape kept only for older non-streaming callers.",
        }
    )


class DiagnosisResponse(BaseModel):
    """Legacy non-streaming response wrapper.

    The current public AIOps path is SSE-first. This model remains for import
    compatibility and generated API examples.
    """

    code: int = 200
    message: str = "success"
    data: dict[str, Any]

    model_config = ConfigDict(
        json_schema_extra={
            "deprecated": True,
            "example": {
                "code": 200,
                "message": "success",
                "data": {
                    "status": "completed",
                    "target_alert": {
                        "alertname": "HighCPUUsage",
                        "severity": "critical",
                    },
                    "diagnosis": {
                        "root_cause": "数据库连接池耗尽",
                        "recommendations": ["扩容数据库连接池", "优化SQL查询"],
                    },
                },
            },
        }
    )
