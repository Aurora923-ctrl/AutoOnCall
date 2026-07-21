"""New diagnosis execution use case for AIOps workflows."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, Protocol

from app.models.incident import Incident


class DiagnosisRunOwner(Protocol):
    async def _execute_diagnosis_run(
        self,
        user_input: str,
        session_id: str | None = None,
        incident: Incident | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]: ...


class AIOpsRun:
    """Expose the complete new-diagnosis use case behind one boundary."""

    def __init__(self, owner: DiagnosisRunOwner):
        self._owner = owner

    def execute(
        self,
        user_input: str,
        session_id: str | None = None,
        incident: Incident | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        return self._owner._execute_diagnosis_run(
            user_input,
            session_id,
            incident,
        )
