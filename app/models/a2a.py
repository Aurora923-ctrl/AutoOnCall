"""Minimal A2A protocol models used by the AutoOnCall facade.

The adapter intentionally models only the HTTP+JSON/SSE surface AutoOnCall
exposes as a remote diagnosis agent. Internal tools, approvals, and change
execution APIs remain on the existing AutoOnCall contracts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

A2A_PROTOCOL_VERSION = "1.0"
A2A_MEDIA_TYPE = "application/a2a+json"

A2ATaskState = Literal[
    "TASK_STATE_UNSPECIFIED",
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_AUTH_REQUIRED",
]


class A2ABaseModel(BaseModel):
    """Base model that supports A2A camelCase fields."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class A2ATextPart(A2ABaseModel):
    """A text part in an A2A message or artifact."""

    text: str
    media_type: str = Field(default="text/plain", alias="mediaType")


class A2ADataPart(A2ABaseModel):
    """A structured data part in an A2A message or artifact."""

    data: dict[str, Any] = Field(default_factory=dict)
    media_type: str = Field(default="application/json", alias="mediaType")


class A2AFilePart(A2ABaseModel):
    """A file/link part in an A2A artifact."""

    file: dict[str, Any] = Field(default_factory=dict)


class A2AMessage(A2ABaseModel):
    """A compact A2A message."""

    message_id: str = Field(alias="messageId")
    role: Literal["ROLE_USER", "ROLE_AGENT"]
    parts: list[dict[str, Any]] = Field(default_factory=list)
    task_id: str | None = Field(default=None, alias="taskId")
    context_id: str | None = Field(default=None, alias="contextId")
    metadata: dict[str, Any] = Field(default_factory=dict)


class A2ATaskStatus(A2ABaseModel):
    """A2A task status."""

    state: A2ATaskState
    message: A2AMessage | None = None
    timestamp: datetime | str | None = None


class A2AArtifact(A2ABaseModel):
    """A2A task artifact."""

    artifact_id: str = Field(alias="artifactId")
    name: str
    description: str = ""
    parts: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class A2ATask(A2ABaseModel):
    """A2A task view backed by an AutoOnCall diagnosis run."""

    id: str
    context_id: str = Field(default="", alias="contextId")
    status: A2ATaskStatus
    artifacts: list[A2AArtifact] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    history: list[A2AMessage] = Field(default_factory=list)


class A2ATaskRecord(BaseModel):
    """Durable ownership and idempotency record for one A2A-created task."""

    task_id: str
    message_id: str
    request_fingerprint: str
    owner_id: str = ""
    skill: str
    incident_id: str = ""
    state: A2ATaskState
    task: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class A2AAgentSkill(A2ABaseModel):
    """One externally callable agent skill."""

    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default_factory=list, alias="inputModes")
    output_modes: list[str] = Field(default_factory=list, alias="outputModes")


class A2AAgentCard(A2ABaseModel):
    """A2A Agent Card used for capability discovery."""

    name: str
    description: str
    supported_interfaces: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="supportedInterfaces",
    )
    provider: dict[str, str] = Field(default_factory=dict)
    version: str
    documentation_url: str = Field(default="", alias="documentationUrl")
    capabilities: dict[str, Any] = Field(default_factory=dict)
    security_schemes: dict[str, Any] = Field(default_factory=dict, alias="securitySchemes")
    security_requirements: list[dict[str, list[str]]] = Field(
        default_factory=list,
        alias="securityRequirements",
    )
    default_input_modes: list[str] = Field(default_factory=list, alias="defaultInputModes")
    default_output_modes: list[str] = Field(default_factory=list, alias="defaultOutputModes")
    skills: list[A2AAgentSkill] = Field(default_factory=list)
