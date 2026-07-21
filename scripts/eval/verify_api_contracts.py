"""Verify AutoOnCall API, SSE, and ToolContract compatibility.

The verifier is intentionally offline and deterministic. It uses FastAPI's
in-process ASGI transport plus fake services so it protects public contracts
without requiring DashScope, Milvus, MCP, Prometheus, Redis, MySQL, or uvicorn.
"""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Awaitable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONTRACT_RUNTIME_DIR = Path(tempfile.mkdtemp(prefix="autooncall-contract-"))
os.environ.setdefault("AIOPS_STORAGE_BACKEND", "sqlite")
os.environ.setdefault("AIOPS_SQLITE_PATH", str(CONTRACT_RUNTIME_DIR / "aiops_state.db"))
os.environ.setdefault("AIOPS_FEEDBACK_PATH", str(CONTRACT_RUNTIME_DIR / "aiops_feedback.jsonl"))
os.environ.setdefault(
    "AIOPS_TOOL_OUTPUT_ARTIFACT_DIR",
    str(CONTRACT_RUNTIME_DIR / "aiops_tool_artifacts"),
)

import httpx
from fastapi import FastAPI

from app.api import aiops, approvals, chat, evaluations, incidents
from app.config import config
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.approval import ApprovalRequest
from app.models.change_execution import ChangeExecution
from app.models.change_plan import ChangePlan
from app.models.incident import Incident
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from scripts.eval.eval_environment import collect_eval_environment, provenance_markdown_lines

DEFAULT_OUTPUT_JSON = ROOT / "logs" / "api_contract_verification.json"
DEFAULT_OUTPUT_MD = ROOT / "logs" / "api_contract_verification.md"

SESSION_ID = "contract-run-001"
INCIDENT_ID = "inc-contract-001"
TRACE_ID = "trace-contract-001"
APPROVAL_ID = "apr-contract-001"
CHANGE_PLAN_ID = "chg-contract-001"
OPERATOR_TOKEN = "contract-operator-token"
APPROVER_TOKEN = OPERATOR_TOKEN
CHANGE_TOKEN = OPERATOR_TOKEN
OPERATOR_PRINCIPAL_ID = sha256(OPERATOR_TOKEN.encode("utf-8")).hexdigest()[:16]
SCOPED_SESSION_ID = f"principal:{OPERATOR_PRINCIPAL_ID}:{SESSION_ID}"

OPERATOR_HEADERS = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
APPROVER_HEADERS = {"Authorization": f"Bearer {APPROVER_TOKEN}"}
CHANGE_HEADERS = {"Authorization": f"Bearer {CHANGE_TOKEN}"}

REQUIRED_PROGRESS_FIELDS = {
    "phase",
    "node_name",
    "current_tool",
    "tool_total",
    "tool_success_count",
    "tool_failed_count",
    "evidence_count",
    "risk_policy",
    "report_status",
    "cursor",
}
REQUIRED_TOOL_CONTRACT_FIELDS = {
    "name",
    "description",
    "input_schema",
    "output_schema",
    "risk_level",
    "read_only",
    "timeout_seconds",
    "retry_policy",
    "data_sources",
    "degradation_strategy",
}
REQUIRED_REPORT_FIELDS = {
    "report_id",
    "incident_id",
    "trace_id",
    "status",
    "summary",
    "root_cause",
    "evidence",
    "tool_calls",
    "evidence_profile",
    "evidence_sufficiency",
    "markdown",
}


class ContractFailure(AssertionError):
    """Raised when one API compatibility check fails."""


@dataclass
class CheckResult:
    """One API compatibility check result."""

    id: str
    name: str
    passed: bool
    details: str = ""
    error: str = ""
    observed: dict[str, Any] | None = None


class FakeRagAgentService:
    """Deterministic RAG service for chat route contract verification."""

    async def query_with_retrieval(
        self,
        question: str,
        session_id: str,
        metadata_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "success": True,
            "answer": "Redis maxclients is close to exhaustion.",
            "citations": [
                {
                    "source_file": "redis_runbook.md",
                    "chunk_id": "redis_runbook.md#0001",
                    "score": 0.11,
                    "content_preview": "Check connected_clients against maxclients.",
                }
            ],
            "retrieval": {
                "status": "success",
                "summary": f"contract fixture for {session_id}",
                "retrieval_results": [
                    {
                        "source_file": "redis_runbook.md",
                        "chunk_id": "redis_runbook.md#0001",
                        "score": 0.11,
                    }
                ],
                "rejected_results": [],
                "no_answer_rejected": False,
                "metadata_filter": metadata_filter,
            },
            "no_answer": False,
            "answer_policy": "answer_with_citations",
        }

    async def query_stream_with_retrieval(
        self,
        question: str,
        session_id: str,
        metadata_filter: dict[str, Any] | None = None,
    ):
        yield {
            "type": "search_results",
            "data": {
                "status": "success",
                "summary": "contract search results",
                "retrieval_results": [
                    {
                        "source_file": "redis_runbook.md",
                        "chunk_id": "redis_runbook.md#0001",
                        "score": 0.11,
                    }
                ],
                "rejected_results": [],
                "no_answer_rejected": False,
                "metadata_filter": metadata_filter,
            },
        }
        yield {"type": "content", "data": "Redis "}
        yield {"type": "content", "data": "maxclients"}
        yield {
            "type": "complete",
            "data": {
                "answer": "Redis maxclients is close to exhaustion.",
                "citations": [
                    {
                        "source_file": "redis_runbook.md",
                        "chunk_id": "redis_runbook.md#0001",
                    }
                ],
                "retrieval": {
                    "status": "success",
                    "retrieval_results": [
                        {
                            "source_file": "redis_runbook.md",
                            "chunk_id": "redis_runbook.md#0001",
                        }
                    ],
                    "rejected_results": [],
                    "no_answer_rejected": False,
                },
                "no_answer": False,
                "answer_policy": "answer_with_citations",
            },
        }

    async def clear_session(self, session_id: str) -> bool:
        return True

    async def get_session_history(self, session_id: str) -> list[dict[str, Any]]:
        return []


class FakeAIOpsService:
    """Deterministic AIOps service for stream and run-status contracts."""

    def __init__(self, report: DiagnosisReport) -> None:
        self.report = report
        self.snapshot = _build_snapshot(report).model_copy(update={"session_id": SCOPED_SESSION_ID})

    async def diagnose(
        self,
        session_id: str | None = None,
        incident: Incident | None = None,
    ):
        session_id = session_id or SESSION_ID
        progress = _progress(
            phase="planning",
            node_name="planner",
            cursor=f"{session_id}:000001",
            status="running",
            current_tool="query_metrics",
        )
        yield _progress_event(progress)
        yield {
            "type": "plan",
            "stage": "planner",
            "status": "running",
            "trace_id": TRACE_ID,
            "incident_id": INCIDENT_ID,
            "plan": ["query_metrics then search_runbook"],
            "current_plan": [
                {
                    "step_id": "s1",
                    "tool_name": "query_metrics",
                    "purpose": "Check error rate",
                }
            ],
            "progress": progress,
            "progress_cursor": progress["cursor"],
        }
        progress = _progress(
            phase="executing",
            node_name="executor",
            cursor=f"{session_id}:000002",
            status="running",
            current_tool="query_metrics",
            tool_total=1,
            tool_success_count=1,
            evidence_count=1,
        )
        yield _progress_event(progress)
        yield {
            "type": "step_complete",
            "stage": "executor",
            "status": "running",
            "trace_id": TRACE_ID,
            "incident_id": INCIDENT_ID,
            "result_preview": "5xx and Redis saturation evidence collected",
            "tool_call_records": [
                {
                    "tool_name": "query_metrics",
                    "status": "success",
                    "data_source": "prometheus",
                }
            ],
            "evidence": [{"source_tool": "query_metrics", "summary": "5xx elevated"}],
            "progress": progress,
            "progress_cursor": progress["cursor"],
        }
        progress = _progress(
            phase="reporting",
            node_name="replanner",
            cursor=f"{session_id}:000003",
            status="completed",
            current_tool="",
            tool_total=1,
            tool_success_count=1,
            evidence_count=1,
            report_status=self.report.status,
        )
        yield _progress_event(progress)
        yield {
            "type": "report",
            "stage": "report",
            "status": self.report.status,
            "trace_id": TRACE_ID,
            "incident_id": INCIDENT_ID,
            "report": self.report.markdown,
            "structured_report": self.report.model_dump(mode="json"),
            "progress": progress,
            "progress_cursor": progress["cursor"],
        }
        progress = _progress(
            phase="complete",
            node_name="workflow",
            cursor=f"{session_id}:000004",
            status=self.report.status,
            current_tool="",
            tool_total=1,
            tool_success_count=1,
            evidence_count=1,
            report_status=self.report.status,
        )
        yield _progress_event(progress)
        yield {
            "type": "complete",
            "stage": "diagnosis_complete",
            "status": self.report.status,
            "message": "diagnosis complete",
            "response": self.report.markdown,
            "incident_id": INCIDENT_ID,
            "trace_id": TRACE_ID,
            "pending_approval": None,
            "risk_assessment": {"policy": "allow", "risk_level": "low"},
            "structured_report": self.report.model_dump(mode="json"),
            "diagnosis": {
                "status": self.report.status,
                "report": self.report.markdown,
                "structured_report": self.report.model_dump(mode="json"),
            },
            "progress": progress,
            "progress_cursor": progress["cursor"],
        }

    async def resume_after_approval(
        self,
        *,
        session_id: str,
        incident_id: str,
        approval: ApprovalRequest,
    ):
        progress = _progress(
            phase="approval",
            node_name="workflow",
            cursor=f"{session_id}:resume-000001",
            status="running",
            report_status="generating",
        )
        yield _progress_event(progress)
        yield {
            "type": "status",
            "stage": "diagnosis_resumed",
            "status": "running",
            "message": "approved decision recorded",
            "incident_id": incident_id,
            "trace_id": TRACE_ID,
            "resume_source": "session_snapshot",
            "execution_boundary": "agent_does_not_execute_production_change",
            "progress": progress,
            "progress_cursor": progress["cursor"],
        }
        progress = _progress(
            phase="complete",
            node_name="workflow",
            cursor=f"{session_id}:resume-000002",
            status="approval_resumed",
            report_status="approval_resumed",
        )
        resumed_report = self.report.model_copy(update={"status": "approval_resumed"})
        yield _progress_event(progress)
        yield {
            "type": "complete",
            "stage": "resume_complete",
            "status": "approval_resumed",
            "message": "approval resume complete",
            "incident_id": incident_id,
            "trace_id": TRACE_ID,
            "resume_source": "session_snapshot",
            "execution_boundary": "agent_does_not_execute_production_change",
            "pending_approval": None,
            "risk_assessment": {"policy": "allow", "approval_id": approval.approval_id},
            "structured_report": resumed_report.model_dump(mode="json"),
            "diagnosis": {
                "status": "approval_resumed",
                "report": resumed_report.markdown,
                "structured_report": resumed_report.model_dump(mode="json"),
            },
            "progress": progress,
            "progress_cursor": progress["cursor"],
        }

    def resolve_resume_session_id(
        self,
        *,
        incident_id: str,
        approval: ApprovalRequest,
        requested_session_id: str | None,
    ) -> str:
        """Apply the production resume identity contract to the fixture."""
        approval_session_id = str(approval.metadata.get("session_id") or "")
        if approval.incident_id != incident_id:
            raise ValueError("approval does not belong to the requested incident")
        if requested_session_id and requested_session_id != approval_session_id:
            raise ValueError("requested session does not match approval session")
        if not approval_session_id:
            raise LookupError("approval is missing its diagnosis session")
        return approval_session_id

    def get_session_snapshot(self, session_id: str) -> AIOpsSessionSnapshot | None:
        return self.snapshot if session_id == self.snapshot.session_id else None

    def list_session_snapshots(
        self,
        *,
        incident_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[AIOpsSessionSnapshot]:
        if incident_id and incident_id != INCIDENT_ID:
            return []
        return [self.snapshot][offset : offset + limit]


class FakeApprovalService:
    """Small mutable approval repository for approval and resume contracts."""

    def __init__(self, approval: ApprovalRequest) -> None:
        self.approvals = {approval.approval_id: approval}

    def get_request(self, approval_id: str) -> ApprovalRequest:
        return self.approvals[approval_id]

    def list_requests(
        self,
        incident_id: str | None = None,
        status: str | None = None,
    ) -> list[ApprovalRequest]:
        items = list(self.approvals.values())
        if incident_id:
            items = [item for item in items if item.incident_id == incident_id]
        if status:
            items = [item for item in items if item.status == status]
        return items

    def list_pending(self, incident_id: str | None = None) -> list[ApprovalRequest]:
        return self.list_requests(incident_id=incident_id, status="pending")

    def decide_request(
        self,
        approval_id: str,
        decision: str,
        decided_by: str = "operator",
        reason: str = "",
    ) -> ApprovalRequest:
        approval = self.get_request(approval_id)
        status = "approved" if decision == "approve" else "rejected"
        updated = approval.model_copy(
            update={
                "status": status,
                "decided_by": decided_by,
                "decision_reason": reason,
                "decided_at": datetime.now(UTC),
            }
        )
        self.approvals[approval_id] = updated
        return updated

    def decide_latest_pending(
        self,
        incident_id: str,
        decision: str,
        decided_by: str = "operator",
        reason: str = "",
    ) -> ApprovalRequest:
        pending = self.list_pending(incident_id=incident_id)
        if not pending:
            raise KeyError(f"No pending approval for incident {incident_id}")
        return self.decide_request(pending[-1].approval_id, decision, decided_by, reason)


class FakeTraceService:
    """Trace read model used by run and incident endpoints."""

    def __init__(self, events: list[TraceEvent]) -> None:
        self.events = events

    def list_events(
        self,
        *,
        incident_id: str | None = None,
        trace_id: str | None = None,
        event_type: str | None = None,
    ) -> list[TraceEvent]:
        return [
            event
            for event in self.events
            if (incident_id is None or event.incident_id == incident_id)
            and (trace_id is None or event.trace_id == trace_id)
            and (event_type is None or event.event_type == event_type)
        ]


class FakeReportGenerator:
    """Report read model used by incident and run endpoints."""

    def __init__(self, report: DiagnosisReport) -> None:
        self.report = report

    def get_report(self, incident_id: str) -> DiagnosisReport | None:
        return self.report if incident_id == self.report.incident_id else None

    def list_reports(self) -> list[DiagnosisReport]:
        return [self.report]


class FakeIncidentStateStore:
    """Incident state store stub for read models that ask for lifecycle state."""

    def __init__(self) -> None:
        self.state = IncidentState(
            incident_id=INCIDENT_ID,
            trace_id=TRACE_ID,
            session_id=SCOPED_SESSION_ID,
            status="waiting_approval",
        )

    def get_incident_state(self, incident_id: str) -> IncidentState | None:
        return self.state if incident_id == INCIDENT_ID else None

    def list_incident_states(self) -> list[Any]:
        return [self.state]


class FakeChangeExecutionService:
    """Safe-change stream stub for resume contract verification."""

    async def start_after_approval(
        self,
        *,
        incident_id: str,
        change_plan_id: str,
        approval_id: str,
        mode: str,
        operator: str,
        observe_window_seconds: int,
    ):
        execution = ChangeExecution(
            change_execution_id="chgexec-contract-001",
            change_plan_id=change_plan_id,
            approval_id=approval_id,
            incident_id=incident_id,
            trace_id=TRACE_ID,
            mode=mode,
            status="dry_run_completed",
            created_by=operator,
        )
        payload = {
            "type": "change_dry_run",
            "stage": "dry_run_completed",
            "status": "passed",
            "message": "dry-run completed without production mutation",
            "incident_id": incident_id,
            "trace_id": TRACE_ID,
            "change_execution": execution.model_dump(mode="json"),
        }
        yield payload
        yield {
            **payload,
            "type": "complete",
            "stage": "change_resume_complete",
            "status": "dry_run_completed",
            "message": "safe change dry-run workflow completed",
        }

    def list_executions(
        self,
        *,
        incident_id: str | None = None,
        change_plan_id: str | None = None,
    ) -> list[ChangeExecution]:
        return []


class PatchSet:
    """Minimal monkeypatch helper for running this script outside pytest."""

    def __init__(self) -> None:
        self._items: list[tuple[Any, str, Any]] = []

    def set(self, target: Any, name: str, value: Any) -> None:
        self._items.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def restore(self) -> None:
        while self._items:
            target, name, old_value = self._items.pop()
            setattr(target, name, old_value)


def parse_sse_events(text: str) -> list[dict[str, Any]]:
    """Parse SSE frames into data payloads plus optional SSE metadata."""
    events: list[dict[str, Any]] = []
    frame: dict[str, Any] = {"data_lines": []}
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            _flush_sse_frame(events, frame)
            frame = {"data_lines": []}
            continue
        if line.startswith("id:"):
            frame["id"] = line.removeprefix("id:").strip()
        elif line.startswith("event:"):
            frame["event"] = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            frame["data_lines"].append(line.removeprefix("data:").strip())
    _flush_sse_frame(events, frame)
    return events


def _flush_sse_frame(events: list[dict[str, Any]], frame: dict[str, Any]) -> None:
    data_lines = frame.get("data_lines") or []
    if not data_lines:
        return
    data_text = "\n".join(data_lines)
    try:
        data = json.loads(data_text)
    except json.JSONDecodeError:
        data = data_text
    events.append(
        {
            "id": frame.get("id", ""),
            "event": frame.get("event", "message"),
            "data": data,
        }
    )


async def verify_api_contracts() -> dict[str, Any]:
    """Run all offline API compatibility checks and return a report payload."""
    report = _build_report()
    approval = _build_approval()
    trace_event = TraceEvent(
        trace_id=TRACE_ID,
        incident_id=INCIDENT_ID,
        node_name="executor",
        event_type="tool_call",
        tool_name="query_metrics",
        status="success",
        output_summary="Redis saturation evidence collected",
    )

    patches = PatchSet()
    checks: list[CheckResult] = []
    with tempfile.TemporaryDirectory(prefix="autooncall-api-contract-") as temp_dir:
        temp_path = Path(temp_dir)
        _configure_contract_auth(patches)
        eval_summary_path, eval_backlog_path, ragas_summary_path = _write_eval_contract_fixtures(
            temp_path
        )
        try:
            app = _build_contract_app(
                patches=patches,
                report=report,
                approval=approval,
                trace_event=trace_event,
                eval_summary_path=eval_summary_path,
                eval_backlog_path=eval_backlog_path,
                ragas_summary_path=ragas_summary_path,
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://contract.test",
                timeout=30,
            ) as client:
                await _capture(checks, "chat_response", "POST /api/chat", _check_chat(client))
                await _capture(
                    checks,
                    "chat_stream_sse",
                    "POST /api/chat_stream SSE",
                    _check_chat_stream(client),
                )
                await _capture(
                    checks,
                    "aiops_sse",
                    "POST /api/aiops SSE",
                    _check_aiops_stream(client),
                )
                await _capture(
                    checks,
                    "aiops_run_status",
                    "GET /api/aiops/runs/{session_id}",
                    _check_aiops_run_status(client),
                )
                await _capture(
                    checks,
                    "tool_contracts",
                    "GET /api/aiops/tools/contracts",
                    _check_tool_contracts(client),
                )
                await _capture(
                    checks,
                    "incident_report_schema",
                    "GET /api/incidents/{incident_id}/report",
                    _check_incident_report(client),
                )
                await _capture(
                    checks,
                    "approval_and_resume",
                    "approval decision + diagnosis resume",
                    _check_approval_and_resume(client),
                )
                await _capture(
                    checks,
                    "safe_change_resume",
                    "safe change resume SSE",
                    _check_safe_change_resume(client),
                )
                await _capture(
                    checks,
                    "eval_summary_backlog",
                    "GET /api/eval/summary and /api/eval/backlog",
                    _check_eval_contracts(client),
                )
                await _capture(
                    checks,
                    "eval_ragas_quality",
                    "GET /api/eval/ragas",
                    _check_ragas_contract(client),
                )
        finally:
            patches.restore()

    return _build_payload(checks)


def _build_contract_app(
    *,
    patches: PatchSet,
    report: DiagnosisReport,
    approval: ApprovalRequest,
    trace_event: TraceEvent,
    eval_summary_path: Path,
    eval_backlog_path: Path,
    ragas_summary_path: Path,
) -> FastAPI:
    fake_aiops = FakeAIOpsService(report)
    fake_approval = FakeApprovalService(approval)
    fake_trace = FakeTraceService([trace_event])
    fake_report = FakeReportGenerator(report)
    fake_change = FakeChangeExecutionService()
    fake_state = FakeIncidentStateStore()

    patches.set(chat, "rag_agent_service", FakeRagAgentService())
    patches.set(aiops, "aiops_service", fake_aiops)
    patches.set(aiops, "get_approval_service", lambda: fake_approval)
    patches.set(aiops, "get_trace_service", lambda: fake_trace)
    patches.set(aiops, "get_report_generator", lambda: fake_report)
    patches.set(aiops, "get_change_execution_service", lambda: fake_change)
    patches.set(approvals, "get_approval_service", lambda: fake_approval)
    patches.set(incidents, "get_approval_service", lambda: fake_approval)
    patches.set(incidents, "get_trace_service", lambda: fake_trace)
    patches.set(incidents, "get_report_generator", lambda: fake_report)
    patches.set(incidents, "get_change_execution_service", lambda: fake_change)
    patches.set(incidents, "get_incident_state_store", lambda: fake_state)
    patches.set(evaluations, "EVAL_SUMMARY_PATH", eval_summary_path)
    patches.set(evaluations, "EVAL_BACKLOG_PATH", eval_backlog_path)
    patches.set(evaluations, "RAGAS_SUMMARY_PATH", ragas_summary_path)

    app = FastAPI(title="AutoOnCall API contract verifier")
    app.include_router(chat.router, prefix="/api")
    app.include_router(aiops.router, prefix="/api")
    app.include_router(approvals.router, prefix="/api")
    app.include_router(incidents.router, prefix="/api")
    app.include_router(evaluations.router, prefix="/api")
    return app


def _configure_contract_auth(patches: PatchSet) -> None:
    """Install deterministic scoped credentials before fixtures capture provenance."""
    patches.set(config, "api_auth_enabled", True)
    patches.set(config, "api_auth_tokens", "")
    patches.set(config, "api_read_token", "")
    patches.set(config, "api_operator_token", OPERATOR_TOKEN)
    patches.set(config, "api_approver_token", APPROVER_TOKEN)
    patches.set(config, "api_change_token", CHANGE_TOKEN)
    patches.set(config, "api_admin_token", "")


async def _capture(
    checks: list[CheckResult],
    check_id: str,
    name: str,
    callback: Awaitable[dict[str, Any]],
) -> None:
    try:
        observed = await callback
        checks.append(
            CheckResult(
                id=check_id,
                name=name,
                passed=True,
                details=observed.pop("details", "contract check passed"),
                observed=observed,
            )
        )
    except Exception as exc:
        checks.append(
            CheckResult(
                id=check_id,
                name=name,
                passed=False,
                error=str(exc),
                observed={},
            )
        )


async def _check_chat(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/chat",
        headers=OPERATOR_HEADERS,
        json={"Id": "contract-rag", "Question": "How to diagnose Redis maxclients?"},
    )
    _require(response.status_code == 200, f"expected 200, got {response.status_code}")
    payload = response.json()
    data = _require_dict(payload.get("data"), "data")
    _require(data.get("success") is True, "data.success must be true")
    _require(isinstance(data.get("answer"), str) and data["answer"], "answer must be non-empty")
    _require_citation_list(data.get("citations"), "data.citations")
    retrieval = _require_dict(data.get("retrieval"), "data.retrieval")
    _require(retrieval.get("status") == "success", "retrieval.status must be success")
    _require(isinstance(data.get("noAnswer"), bool), "noAnswer must be boolean")
    _require(data.get("answerPolicy"), "answerPolicy must be present")
    return {
        "details": "chat response keeps answer, citations, retrieval, noAnswer, answerPolicy",
        "answerPolicy": data.get("answerPolicy"),
        "citation_count": len(data.get("citations") or []),
    }


async def _check_chat_stream(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/chat_stream",
        headers=OPERATOR_HEADERS,
        json={
            "Id": "contract-rag-stream",
            "Question": "Stream Redis diagnosis",
            "metadataFilter": {"doc_type": "runbook"},
        },
    )
    _require(response.status_code == 200, f"expected 200, got {response.status_code}")
    events = parse_sse_events(response.text)
    payloads = [_require_dict(event.get("data"), "sse.data") for event in events]
    event_types = [str(payload.get("type") or "") for payload in payloads]
    for required in ["search_results", "content", "done"]:
        _require(required in event_types, f"missing chat stream event type: {required}")
    done = payloads[-1]
    _require(done["type"] == "done", "last chat stream event must be done")
    done_data = _require_dict(done.get("data"), "done.data")
    _require_citation_list(done_data.get("citations"), "done.data.citations")
    _require_dict(done_data.get("retrieval"), "done.data.retrieval")
    return {
        "details": "chat_stream emits search_results/content/done with grounded payload",
        "event_types": event_types,
    }


async def _check_aiops_stream(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/aiops",
        headers=OPERATOR_HEADERS,
        json={
            "session_id": SESSION_ID,
            "incident": {
                "incident_id": INCIDENT_ID,
                "title": "order-service Redis maxclients exhausted",
                "service_name": "order-service",
                "severity": "P1",
                "symptom": "Redis timeouts and 5xx",
                "environment": "prod",
            },
        },
    )
    _require(response.status_code == 200, f"expected 200, got {response.status_code}")
    events = parse_sse_events(response.text)
    payloads = [_require_dict(event.get("data"), "sse.data") for event in events]
    event_types = [str(payload.get("type") or "") for payload in payloads]
    for required in ["progress", "plan", "step_complete", "report", "complete"]:
        _require(required in event_types, f"missing aiops stream event type: {required}")
    for payload in payloads:
        if payload.get("progress"):
            _require_progress(payload["progress"])
    progress_frames = [event for event in events if event["data"].get("type") == "progress"]
    _require(
        any(event.get("id") == event["data"].get("progress_cursor") for event in progress_frames),
        "progress SSE frames must expose cursor as event id",
    )
    complete = payloads[-1]
    _require(complete.get("type") == "complete", "last AIOps event must be complete")
    structured_report = _require_dict(
        complete.get("structured_report"), "complete.structured_report"
    )
    _require(complete.get("status") == structured_report.get("status"), "terminal status mismatch")
    diagnosis = _require_dict(complete.get("diagnosis"), "complete.diagnosis")
    _require(
        diagnosis.get("status") == complete.get("status"),
        "diagnosis.status must mirror terminal status",
    )
    return {
        "details": "AIOps SSE exposes progress, terminal report, and status fields",
        "event_types": event_types,
        "terminal_status": complete.get("status"),
    }


async def _check_aiops_run_status(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get(
        f"/api/aiops/runs/{SESSION_ID}",
        headers=OPERATOR_HEADERS,
    )
    _require(response.status_code == 200, f"expected 200, got {response.status_code}")
    payload = response.json()
    _require(payload.get("session_id") == SCOPED_SESSION_ID, "session_id mismatch")
    _require(payload.get("incident_id") == INCIDENT_ID, "incident_id mismatch")
    _require(payload.get("trace_id") == TRACE_ID, "trace_id mismatch")
    progress = _require_dict(payload.get("progress"), "progress")
    _require_progress(progress)
    _require(payload.get("progress_cursor") == progress["cursor"], "progress_cursor mismatch")
    _require(isinstance(payload.get("progress_events"), list), "progress_events must be a list")
    _require_dict(payload.get("trace_summary"), "trace_summary")
    _require_dict(payload.get("approval_summary"), "approval_summary")
    return {
        "details": "run status exposes durable progress recovery and read-model links",
        "status": payload.get("status"),
        "progress_cursor": payload.get("progress_cursor"),
    }


async def _check_tool_contracts(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/aiops/tools/contracts", headers=OPERATOR_HEADERS)
    _require(response.status_code == 200, f"expected 200, got {response.status_code}")
    payload = response.json()
    items = payload.get("items")
    _require(isinstance(items, list) and items, "items must be a non-empty list")
    _require(payload.get("count") == len(items), "count must match items length")
    names = {str(item.get("name") or "") for item in items if isinstance(item, dict)}
    for required_tool in ["query_metrics", "query_redis_status", "search_runbook"]:
        _require(required_tool in names, f"missing tool contract: {required_tool}")
    for item in items:
        item = _require_dict(item, "tool contract item")
        _require_keys(item, REQUIRED_TOOL_CONTRACT_FIELDS, f"tool contract {item.get('name')}")
        _require(isinstance(item.get("read_only"), bool), "read_only must be boolean")
        _require(float(item.get("timeout_seconds") or 0) > 0, "timeout_seconds must be positive")
        _require_dict(item.get("retry_policy"), "retry_policy")
    return {
        "details": "tool contracts expose auditable fields without invoking adapters",
        "tool_count": len(items),
        "tools": sorted(names),
    }


async def _check_incident_report(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get(
        f"/api/incidents/{INCIDENT_ID}/report",
        headers=OPERATOR_HEADERS,
    )
    _require(response.status_code == 200, f"expected 200, got {response.status_code}")
    payload = response.json()
    report = _require_dict(payload.get("report"), "report")
    _require_keys(report, REQUIRED_REPORT_FIELDS, "report")
    _require(payload.get("incident_id") == report["incident_id"], "incident_id mismatch")
    _require(payload.get("trace_id") == report["trace_id"], "trace_id mismatch")
    _require(isinstance(payload.get("markdown"), str), "markdown must be string")
    _require(isinstance(report.get("evidence"), list), "report.evidence must be list")
    _require(isinstance(report.get("tool_calls"), list), "report.tool_calls must be list")
    return {
        "details": "incident report keeps structured report plus markdown",
        "report_status": report.get("status"),
        "evidence_count": len(report.get("evidence") or []),
    }


async def _check_approval_and_resume(client: httpx.AsyncClient) -> dict[str, Any]:
    decision_response = await client.post(
        f"/api/incidents/{INCIDENT_ID}/approval",
        headers=APPROVER_HEADERS,
        json={
            "approval_id": APPROVAL_ID,
            "decision": "approve",
            "decided_by": "contract-verifier",
            "reason": "contract verification",
        },
    )
    _require(
        decision_response.status_code == 200,
        f"expected approval 200, got {decision_response.status_code}",
    )
    approval_payload = _require_dict(decision_response.json().get("approval"), "approval")
    _require(approval_payload.get("status") == "approved", "approval must be approved")
    _require(approval_payload.get("approval_id") == APPROVAL_ID, "approval_id mismatch")

    resume_response = await client.post(
        f"/api/incidents/{INCIDENT_ID}/diagnosis/resume",
        headers=OPERATOR_HEADERS,
        json={"session_id": SESSION_ID, "approval_id": APPROVAL_ID},
    )
    _require(
        resume_response.status_code == 200,
        f"expected resume 200, got {resume_response.status_code}",
    )
    events = parse_sse_events(resume_response.text)
    payloads = [_require_dict(event.get("data"), "resume.sse.data") for event in events]
    complete = payloads[-1]
    _require(complete.get("type") == "complete", "resume stream must end with complete")
    _require(
        complete.get("execution_boundary") == "agent_does_not_execute_production_change",
        "resume must keep production change boundary",
    )
    _require_dict(complete.get("structured_report"), "resume structured_report")
    _require_progress(complete.get("progress"))
    return {
        "details": "approval decision and diagnosis resume preserve safety boundary",
        "resume_event_types": [payload.get("type") for payload in payloads],
        "approval_status": approval_payload.get("status"),
    }


async def _check_safe_change_resume(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        f"/api/incidents/{INCIDENT_ID}/changes/{CHANGE_PLAN_ID}/resume",
        headers=CHANGE_HEADERS,
        json={
            "approval_id": APPROVAL_ID,
            "mode": "dry_run_only",
            "operator": "contract-verifier",
            "observe_window_seconds": 60,
        },
    )
    _require(response.status_code == 200, f"expected 200, got {response.status_code}")
    events = parse_sse_events(response.text)
    payloads = [_require_dict(event.get("data"), "change.sse.data") for event in events]
    event_types = [str(payload.get("type") or "") for payload in payloads]
    _require("change_dry_run" in event_types, "change_dry_run event missing")
    complete = payloads[-1]
    _require(complete.get("type") == "complete", "change resume must end with complete")
    execution = _require_dict(complete.get("change_execution"), "change_execution")
    for field in ["change_execution_id", "change_plan_id", "approval_id", "status", "mode"]:
        _require(field in execution, f"change_execution.{field} missing")
    _require(execution.get("mode") == "dry_run_only", "mode must stay dry_run_only")
    return {
        "details": "safe change resume exposes dry-run execution fields",
        "event_types": event_types,
        "change_status": execution.get("status"),
    }


async def _check_eval_contracts(client: httpx.AsyncClient) -> dict[str, Any]:
    summary_response = await client.get("/api/eval/summary", headers=OPERATOR_HEADERS)
    _require(summary_response.status_code == 200, "eval summary must return 200")
    summary_payload = summary_response.json()
    _require(summary_payload.get("available") is True, "eval summary must be available")
    backlog = _require_dict(summary_payload.get("eval_backlog"), "eval_backlog")
    _require_dict(backlog.get("summary"), "eval_backlog.summary")
    _require(isinstance(backlog.get("items"), list), "eval_backlog.items must be list")

    backlog_response = await client.get("/api/eval/backlog", headers=OPERATOR_HEADERS)
    _require(backlog_response.status_code == 200, "eval backlog must return 200")
    backlog_payload = backlog_response.json()
    _require(backlog_payload.get("available") is True, "eval backlog must be available")
    _require_dict(backlog_payload.get("summary"), "eval backlog summary")
    _require(isinstance(backlog_payload.get("items"), list), "eval backlog items must be list")
    return {
        "details": "eval summary and backlog expose reviewable bad-case counters",
        "backlog_total": backlog_payload["summary"].get("total", 0),
    }


async def _check_ragas_contract(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/eval/ragas", headers=OPERATOR_HEADERS)
    _require(response.status_code == 200, "RAGAS summary must return 200")
    payload = response.json()
    _require(payload.get("available") is True, "RAGAS summary must be available")
    dashboard = _require_dict(payload.get("dashboard"), "ragas.dashboard")
    summary = _require_dict(payload.get("summary"), "ragas.summary")
    _require(dashboard.get("profile") == "id-smoke", "RAGAS profile must be exposed")
    _require("--metrics-profile id-smoke" in str(dashboard.get("command")), "command hint missing")
    metrics = dashboard.get("metrics")
    _require(isinstance(metrics, list) and metrics, "ragas.dashboard.metrics must be non-empty")
    metric_keys = {str(item.get("key") or "") for item in metrics if isinstance(item, dict)}
    for required_metric in ["ragas_id_recall", "ragas_actionability", "ragas_refusal_boundary"]:
        _require(
            required_metric in metric_keys, f"missing RAGAS dashboard metric {required_metric}"
        )
    case_scores = payload.get("case_scores")
    _require(isinstance(case_scores, list) and case_scores, "ragas.case_scores must be non-empty")
    _require(summary.get("status") == "passed", "RAGAS fixture should pass")
    return {
        "details": "RAGAS answer-quality dashboard exposes reproducible quality gates",
        "profile": dashboard.get("profile"),
        "case_count": summary.get("case_count", 0),
        "metrics": sorted(metric_keys),
    }


def _build_report() -> DiagnosisReport:
    report = DiagnosisReport(
        report_id="rpt-contract-001",
        incident_id=INCIDENT_ID,
        trace_id=TRACE_ID,
        title="order-service Redis maxclients exhausted",
        service_name="order-service",
        severity="P1",
        environment="prod",
        status="completed",
        summary="Redis connected_clients is near maxclients and API 5xx increased.",
        root_cause="Redis maxclients capacity exhausted.",
        hypotheses=["Redis connection saturation"],
        evidence=[
            {
                "evidence_id": "ev-contract-001",
                "source_tool": "query_redis_status",
                "data_source": "redis_info",
                "summary": "connected_clients=9940/maxclients=10000",
                "fact": "Redis clients are close to the configured limit.",
            }
        ],
        key_findings=["Redis clients are close to maxclients."],
        confirmed_facts=["connected_clients=9940/maxclients=10000"],
        inferred_conclusions=["Redis maxclients exhaustion is plausible."],
        next_steps=["Keep diagnosis read-only and use approval for changes."],
        tool_calls=[
            {
                "tool_name": "query_redis_status",
                "status": "success",
                "data_source": "redis_info",
                "read_only": True,
            }
        ],
        risk_summary={"policy": "allow", "risk_level": "low", "need_approval": False},
        evidence_profile={
            "source_quality": "trusted",
            "by_layer": {"live": 1, "knowledge": 1, "history": 0, "other": 0},
            "artifact_count": 0,
        },
        evidence_sufficiency={"complete": True, "missing_evidence": []},
        confidence=0.82,
    )
    return report.model_copy(
        update={"markdown": "# order-service Redis maxclients exhausted\n\nContract report."}
    )


def _build_approval() -> ApprovalRequest:
    change_plan = ChangePlan(
        change_plan_id=CHANGE_PLAN_ID,
        incident_id=INCIDENT_ID,
        action="Increase Redis maxclients after approval",
        risk_level="medium",
        status="approved",
        execution_steps=["Record operator-approved Redis config change"],
        rollback_steps=["Restore previous maxclients value"],
        observe_metrics=["redis_connected_clients", "http_5xx_rate"],
        metadata={"environment": "prod", "service_name": "order-service"},
    )
    return ApprovalRequest(
        approval_id=APPROVAL_ID,
        incident_id=INCIDENT_ID,
        action=change_plan.action,
        risk_level="medium",
        reason="Capacity remediation requires approval.",
        status="pending",
        tool_name="suggest_remediation",
        change_plan=change_plan,
        metadata={
            "trace_id": TRACE_ID,
            "session_id": SCOPED_SESSION_ID,
            "change_plan": change_plan.model_dump(mode="json"),
        },
    )


def _build_snapshot(report: DiagnosisReport) -> AIOpsSessionSnapshot:
    progress = _progress(
        phase="executing",
        node_name="executor",
        cursor=f"{SESSION_ID}:000002",
        status="running",
        current_tool="query_metrics",
        tool_total=1,
        tool_success_count=1,
        evidence_count=1,
    )
    return AIOpsSessionSnapshot.from_state(
        session_id=SESSION_ID,
        status="running",
        node_name="executor",
        state={
            "session_id": SESSION_ID,
            "input": "diagnose Redis maxclients",
            "trace_id": TRACE_ID,
            "incident": {
                "incident_id": INCIDENT_ID,
                "title": report.title,
                "service_name": report.service_name,
                "severity": report.severity,
                "environment": report.environment,
                "symptom": "Redis timeouts and 5xx",
            },
            "current_plan": [
                {"step_id": "s1", "tool_name": "query_metrics", "purpose": "Check 5xx"}
            ],
            "past_steps": [({"step_id": "s1", "tool_name": "query_metrics"}, "ok")],
            "tool_call_records": report.tool_calls,
            "gathered_evidence": report.evidence,
            "report": report.model_dump(mode="json"),
            "progress": progress,
            "progress_cursor": progress["cursor"],
            "progress_events": [progress],
        },
    )


def _write_eval_contract_fixtures(temp_path: Path) -> tuple[Path, Path, Path]:
    eval_summary_path = temp_path / "eval_summary.json"
    eval_backlog_path = temp_path / "eval_backlog_drafts.json"
    ragas_summary_path = temp_path / "ragas_eval_summary.json"
    eval_environment = collect_eval_environment(
        suite="api_contract_fixture",
        evidence_level="offline_fixture",
    )
    ragas_environment = collect_eval_environment(
        suite="api_contract_ragas_fixture",
        evidence_level="offline_fixture",
    )
    backlog_item = {
        "backlog_id": "ebl-contract-001",
        "feedback_id": "fbk-contract-001",
        "source": "direct_feedback",
        "target": "aiops",
        "category": "tool_failure",
        "priority": "P0",
        "review_status": "new",
        "suggested_eval_file": "eval/cases.yaml",
        "suggested_eval_case_id": "draft_aiops_contract_tool_failure",
        "suggested_eval_dimension": "tool_failure_graceful_degradation",
        "expected_behavior": "Failed tools should produce degraded reports.",
        "failure_reasons": ["contract fixture"],
        "evidence_snapshot": {"trace_id": TRACE_ID},
        "links": {"incident_id": INCIDENT_ID},
    }
    eval_summary_path.write_text(
        json.dumps(
            {
                "run": {
                    "started_at": "2026-07-08T00:00:00Z",
                    "evaluation_scope": "api_contract_fixture",
                    "environment": eval_environment,
                },
                "summary": {
                    "overall_case_count": 1,
                    "overall_pass_rate": 1.0,
                    "failed_cases": [],
                    "resume_metrics": {"aiops_pass_rate": 1.0},
                },
                "cases": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    eval_backlog_path.write_text(
        json.dumps(
            {
                "summary": {
                    "total": 1,
                    "by_target": {"aiops": 1},
                    "by_category": {"tool_failure": 1},
                    "by_priority": {"P0": 1},
                    "by_review_status": {"new": 1},
                    "by_eval_file": {"eval/cases.yaml": 1},
                },
                "items": [backlog_item],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    ragas_summary_path.write_text(
        json.dumps(
            {
                "run": {
                    "ended_at": "2026-07-08T00:00:00Z",
                    "evaluation_scope": "api_contract_ragas_fixture",
                    "environment": ragas_environment,
                    "cases_path": "eval/rag_cases.yaml",
                    "docs_dir": "docs/knowledge-base",
                    "answer_source": "reference-fixture",
                    "metric_profile": "id-smoke",
                    "judge_model": "qwen-max",
                    "embedding_model": "text-embedding-v4",
                    "artifacts": {
                        "summary_json": "logs/ragas_eval_summary.json",
                        "summary_md": "logs/ragas_eval_summary.md",
                    },
                },
                "thresholds": {
                    "id_context_recall": 0.75,
                    "oncall_actionability": 0.8,
                    "refusal_boundary_rate": 1.0,
                },
                "summary": {
                    "status": "passed",
                    "case_count": 2,
                    "quality_case_count": 1,
                    "refusal_case_count": 1,
                    "passed_count": 2,
                    "pass_rate": 1.0,
                    "core_case_count": 2,
                    "core_case_pass_rate": 1.0,
                    "id_context_precision_avg": 1.0,
                    "id_context_recall_avg": 1.0,
                    "oncall_actionability_avg": 1.0,
                    "refusal_boundary_rate": 1.0,
                    "faithfulness_avg": 0.0,
                    "response_relevancy_avg": 0.0,
                    "failed_cases": [],
                },
                "case_scores": [
                    {
                        "id": "redis_contract_quality",
                        "case_type": "positive",
                        "tags": ["core_interview"],
                        "core_case": True,
                        "passed": True,
                        "metrics": {
                            "id_based_context_recall": 1.0,
                            "oncall_actionability_score": 1.0,
                        },
                        "failed_metrics": [],
                    },
                    {
                        "id": "reject_contract_boundary",
                        "case_type": "negative",
                        "tags": ["core_interview", "refusal_boundary"],
                        "core_case": True,
                        "should_reject": True,
                        "passed": True,
                        "metrics": {"refusal_boundary_hit": True},
                        "failed_metrics": [],
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return eval_summary_path, eval_backlog_path, ragas_summary_path


def _progress(
    *,
    phase: str,
    node_name: str,
    cursor: str,
    status: str,
    current_tool: str = "",
    tool_total: int = 0,
    tool_success_count: int = 0,
    tool_failed_count: int = 0,
    evidence_count: int = 0,
    risk_policy: str = "allow",
    report_status: str = "not_started",
) -> dict[str, Any]:
    return {
        "phase": phase,
        "node_name": node_name,
        "current_tool": current_tool,
        "tool_total": tool_total,
        "tool_success_count": tool_success_count,
        "tool_failed_count": tool_failed_count,
        "evidence_count": evidence_count,
        "risk_policy": risk_policy,
        "report_status": report_status,
        "cursor": cursor,
        "session_id": SESSION_ID,
        "status": status,
        "message": f"{phase} progress",
        "incident_id": INCIDENT_ID,
        "trace_id": TRACE_ID,
    }


def _progress_event(progress: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "progress",
        "stage": progress["phase"],
        "status": progress["status"],
        "message": progress["message"],
        **progress,
        "progress": dict(progress),
        "progress_cursor": progress["cursor"],
    }


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ContractFailure(message)


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    _require(isinstance(value, dict), f"{path} must be an object")
    return value


def _require_keys(value: dict[str, Any], required: set[str], path: str) -> None:
    missing = sorted(required.difference(value))
    _require(not missing, f"{path} missing keys: {', '.join(missing)}")


def _require_citation_list(value: Any, path: str) -> None:
    _require(isinstance(value, list) and value, f"{path} must be a non-empty list")
    for index, item in enumerate(value):
        item = _require_dict(item, f"{path}[{index}]")
        _require(item.get("source_file"), f"{path}[{index}].source_file is required")
        _require(item.get("chunk_id"), f"{path}[{index}].chunk_id is required")


def _require_progress(value: Any) -> None:
    progress = _require_dict(value, "progress")
    _require_keys(progress, REQUIRED_PROGRESS_FIELDS, "progress")
    _require(isinstance(progress.get("tool_total"), int), "progress.tool_total must be int")
    _require(isinstance(progress.get("evidence_count"), int), "progress.evidence_count must be int")
    _require(progress.get("cursor"), "progress.cursor must be non-empty")


def _build_payload(checks: list[CheckResult]) -> dict[str, Any]:
    passed_count = sum(1 for check in checks if check.passed)
    failed = [check.id for check in checks if not check.passed]
    return {
        "run": {
            "generated_at": datetime.now(UTC).isoformat(),
            "scope": (
                "offline ASGI API/SSE/ToolContract compatibility verification; "
                "fake services only, no external dependencies"
            ),
            "external_dependencies": False,
            "environment": collect_eval_environment(suite="api_contract"),
        },
        "summary": {
            "status": "passed" if not failed else "failed",
            "check_count": len(checks),
            "passed_check_count": passed_count,
            "failed_check_count": len(failed),
            "failed_checks": failed,
        },
        "checks": [
            {
                **asdict(check),
                "observed": check.observed or {},
            }
            for check in checks
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    """Render a concise Markdown verification report."""
    summary = payload["summary"]
    lines = [
        "# AutoOnCall API Contract Verification",
        "",
        "## Summary",
        "",
        f"- Status: `{summary['status']}`",
        f"- Checks: `{summary['passed_check_count']}/{summary['check_count']}`",
        f"- External dependencies: `{payload['run']['external_dependencies']}`",
        f"- Scope: {payload['run']['scope']}",
        *provenance_markdown_lines(payload["run"]["environment"]),
        "",
        "## Checks",
        "",
        "| Check | Status | Details |",
        "| --- | --- | --- |",
    ]
    for check in payload["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        details = check["details"] if check["passed"] else check["error"]
        lines.append(f"| `{check['id']}` | `{status}` | {details} |")
    lines.append("")
    return "\n".join(lines)


def write_outputs(payload: dict[str, Any], json_path: Path, md_path: Path) -> None:
    """Write JSON and Markdown reports."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), "utf-8")
    md_path.write_text(render_markdown(payload), "utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--summary-md", default=str(DEFAULT_OUTPUT_MD))
    parser.add_argument("--json", action="store_true", help="Print the full JSON report")
    return parser.parse_args(argv)


async def main_async(argv: list[str] | None = None) -> dict[str, Any]:
    args = parse_args(argv)
    payload = await verify_api_contracts()
    write_outputs(payload, Path(args.summary_json), Path(args.summary_md))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        summary = payload["summary"]
        print(
            "API contract verification: "
            f"{summary['status']}; "
            f"checks={summary['passed_check_count']}/{summary['check_count']}; "
            f"report={args.summary_json}"
        )
    return payload


def main(argv: list[str] | None = None) -> int:
    payload = asyncio.run(main_async(argv))
    return 0 if payload["summary"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
