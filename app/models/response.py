"""响应数据模型

定义 API 响应的 Pydantic 模型
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatResponse(BaseModel):
    """对话响应"""

    answer: str = Field(..., description="AI 回答")

    session_id: str = Field(..., description="会话 ID")


class ChatDataResponse(BaseModel):
    """Data payload returned by the non-streaming chat endpoint."""

    model_config = ConfigDict(populate_by_name=True)

    success: bool
    answer: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    retrieval: dict[str, Any] = Field(default_factory=dict)
    observability: dict[str, Any] = Field(default_factory=dict)
    no_answer: bool = Field(default=False, alias="noAnswer")
    answer_policy: str = Field(default="", alias="answerPolicy")
    error_message: str | None = Field(default=None, alias="errorMessage")


class ChatApiResponse(BaseModel):
    """Envelope returned by the non-streaming chat endpoint."""

    code: int
    message: str
    data: ChatDataResponse


class SessionInfoResponse(BaseModel):
    """会话信息响应"""

    session_id: str = Field(..., description="会话 ID")

    message_count: int = Field(..., description="消息数量")

    history: list[dict[str, str]] = Field(..., description="历史消息列表")


class ApiResponse(BaseModel):
    """通用 API 响应"""

    status: str = Field(..., description="状态")

    message: str = Field(..., description="消息")

    data: Any | None = Field(None, description="数据")


class HealthResponse(BaseModel):
    """健康检查响应"""

    status: str = Field(..., description="状态")

    service: str = Field(..., description="服务名称")

    version: str = Field(..., description="版本号")


class HealthDataResponse(BaseModel):
    """Shared liveness/readiness payload."""

    service: str
    version: str
    status: str
    mode: str
    checks: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    milvus: dict[str, Any] | None = None
    selected_capability: str | None = None
    unready_capabilities: list[str] = Field(default_factory=list)
    error: str | None = None


class HealthApiResponse(BaseModel):
    """Envelope returned by liveness and readiness endpoints."""

    code: int
    message: str
    data: HealthDataResponse
