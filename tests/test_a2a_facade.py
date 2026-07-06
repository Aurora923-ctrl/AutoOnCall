"""Tests for the business-scoped A2A facade."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api import a2a
from app.config import config
from app.models.aiops_session import AIOpsSessionSnapshot


class FakeAIOpsService:
    def __init__(self) -> None:
        self.snapshots: dict[str, AIOpsSessionSnapshot] = {}

    async def diagnose(self, session_id: str, incident: Any):
        snapshot = AIOpsSessionSnapshot.from_state(
            session_id=session_id,
            status="completed",
            node_name="workflow",
            state={
                "input": incident.symptom,
                "trace_id": "trace-a2a",
                "incident": incident.model_dump(mode="json"),
                "gathered_evidence": [
                    {
                        "source_tool": "query_redis_status",
                        "summary": "Redis connected_clients 接近 maxclients",
                    }
                ],
                "report": {
                    "report_id": "rpt-a2a",
                    "incident_id": incident.incident_id,
                    "trace_id": "trace-a2a",
                    "status": "completed",
                    "markdown": "# A2A diagnosis report",
                    "root_cause": "Redis maxclients exhausted",
                },
                "response": "# A2A diagnosis report",
            },
        )
        self.snapshots[session_id] = snapshot
        yield {
            "type": "complete",
            "status": "completed",
            "message": "诊断流程完成",
            "incident_id": incident.incident_id,
            "trace_id": "trace-a2a",
            "structured_report": snapshot.report,
        }

    def get_session_snapshot(self, session_id: str) -> AIOpsSessionSnapshot | None:
        return self.snapshots.get(session_id)

    def list_session_snapshots(
        self,
        *,
        incident_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[AIOpsSessionSnapshot]:
        snapshots = [
            snapshot
            for snapshot in self.snapshots.values()
            if incident_id is None or snapshot.incident_id == incident_id
        ]
        return snapshots[offset : offset + limit]


class FakeTraceService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def list_events(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        return []


class FakeApprovalService:
    def list_requests(self, **_kwargs: Any) -> list[Any]:
        return []


class FakeReportGenerator:
    def get_report(self, _incident_id: str) -> None:
        return None


class FakeChangeExecutionService:
    def list_executions(self, **_kwargs: Any) -> list[Any]:
        return []


class FakeIncidentStateStore:
    def get_incident_state(self, _incident_id: str) -> None:
        return None


class FakeRagAgentService:
    async def query_with_retrieval(
        self,
        question: str,
        session_id: str,
        metadata_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "success": True,
            "answer": f"Answer for {question}",
            "citations": [{"source_file": "redis.md", "chunk_id": "chunk-1"}],
            "retrieval": {"retrieval_results": [{"source_file": "redis.md"}]},
            "no_answer": False,
            "answer_policy": "answer_with_citations",
            "session_id": session_id,
            "metadata_filter": metadata_filter,
        }


def build_test_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    auth_enabled: bool = False,
    read_token: str = "",
    operator_token: str = "",
) -> FastAPI:
    monkeypatch.setattr(config, "a2a_enabled", enabled)
    monkeypatch.setattr(config, "api_auth_enabled", auth_enabled)
    monkeypatch.setattr(config, "api_read_token", read_token)
    monkeypatch.setattr(config, "api_operator_token", operator_token)
    monkeypatch.setattr(config, "api_approver_token", "")
    monkeypatch.setattr(config, "api_admin_token", "")
    monkeypatch.setattr(config, "api_auth_tokens", "")
    app = FastAPI()
    app.include_router(a2a.discovery_router)
    app.include_router(a2a.router, prefix=config.normalized_a2a_base_path)
    return app


def install_fake_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeAIOpsService, FakeTraceService]:
    from app.services.a2a_facade import A2AFacade

    fake_aiops = FakeAIOpsService()
    fake_trace = FakeTraceService()
    facade = A2AFacade(
        aiops_service=fake_aiops,
        trace_service=fake_trace,
        approval_service=FakeApprovalService(),
        report_generator=FakeReportGenerator(),
        change_execution_service=FakeChangeExecutionService(),
        rag_agent_service=FakeRagAgentService(),
        incident_state_store=FakeIncidentStateStore(),
    )
    monkeypatch.setattr(a2a, "a2a_facade", facade)
    return fake_aiops, fake_trace


@pytest.mark.asyncio
async def test_a2a_agent_card_is_disabled_by_default(monkeypatch) -> None:
    app = build_test_app(monkeypatch, enabled=False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/.well-known/agent-card.json")

    assert response.status_code == 404
    assert response.json()["detail"] == "A2A adapter is disabled"


@pytest.mark.asyncio
async def test_a2a_agent_card_exposes_only_business_skills(monkeypatch) -> None:
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/a2a+json")
    body = response.json()
    assert body["protocolVersion"] == "1.0"
    skill_ids = {item["id"] for item in body["skills"]}
    assert skill_ids == {
        "diagnose_incident",
        "get_incident_status",
        "explain_incident_replay",
        "answer_runbook_question",
    }
    assert "query_metrics" not in skill_ids
    assert "query_redis_status" not in skill_ids


@pytest.mark.asyncio
async def test_a2a_message_send_runs_incident_diagnosis_as_task(monkeypatch) -> None:
    _, fake_trace = install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:send",
            json={
                "message": {
                    "messageId": "msg-diagnose",
                    "role": "user",
                    "parts": [
                        {
                            "data": {
                                "skill": "diagnose_incident",
                                "incident": {
                                    "incident_id": "inc-a2a",
                                    "title": "order-service Redis timeout",
                                    "service_name": "order-service",
                                    "severity": "P1",
                                    "environment": "prod",
                                    "symptom": "Redis timeout and 5xx spike",
                                },
                            },
                        }
                    ],
                }
            },
        )

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["id"].startswith("a2a-diagnosis-")
    assert task["contextId"] == "inc-a2a"
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["status"]["message"]["role"] == "ROLE_AGENT"
    assert task["metadata"]["skill"] == "diagnose_incident"
    artifact_ids = {artifact["artifactId"] for artifact in task["artifacts"]}
    assert "rpt-a2a" in artifact_ids
    assert "evidence" in artifact_ids
    assert "kind" not in task["artifacts"][0]["parts"][0]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        task_response = await client.get(f"/a2a/v1/tasks/{task['id']}")

    assert task_response.status_code == 200
    assert task_response.json()["id"] == task["id"]
    assert {"trace_id": "trace-a2a"} in fake_trace.calls


@pytest.mark.asyncio
async def test_a2a_message_send_supports_cited_runbook_answers(monkeypatch) -> None:
    install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:send",
            json={
                "message": {
                    "messageId": "msg-rag",
                    "role": "user",
                    "parts": [
                        {
                            "data": {
                                "skill": "answer_runbook_question",
                                "question": "Redis maxclients 告警如何排查？",
                            },
                        }
                    ],
                }
            },
        )

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["id"].startswith("a2a-runbook-")
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["metadata"]["skill"] == "answer_runbook_question"
    assert task["metadata"]["answer_policy"] == "answer_with_citations"
    artifact = task["artifacts"][0]
    assert artifact["artifactId"] == "runbook_answer"
    assert artifact["parts"][0]["text"].startswith("Answer for")
    assert "kind" not in artifact["parts"][0]
    assert artifact["parts"][1]["data"]["citations"][0]["source_file"] == "redis.md"


@pytest.mark.asyncio
async def test_a2a_diagnosis_ignores_client_supplied_task_id(monkeypatch) -> None:
    fake_aiops, _ = install_fake_facade(monkeypatch)
    fake_aiops.snapshots["existing-task"] = AIOpsSessionSnapshot.from_state(
        session_id="existing-task",
        status="completed",
        node_name="workflow",
        state={
            "input": "existing sensitive diagnosis",
            "trace_id": "trace-existing",
            "incident": {
                "incident_id": "inc-existing",
                "title": "existing task",
                "symptom": "old symptom",
            },
        },
    )
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:send",
            json={
                "message": {
                    "messageId": "msg-collision",
                    "taskId": "existing-task",
                    "role": "ROLE_USER",
                    "parts": [
                        {
                            "data": {
                                "skill": "diagnose_incident",
                                "incident": {
                                    "incident_id": "inc-new",
                                    "title": "new task",
                                    "symptom": "new symptom",
                                },
                            }
                        }
                    ],
                }
            },
        )

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["id"] != "existing-task"
    assert task["id"].startswith("a2a-diagnosis-")
    assert task["contextId"] == "inc-new"
    assert task["metadata"]["incident_id"] == "inc-new"


@pytest.mark.asyncio
async def test_a2a_message_auth_uses_read_scope_for_read_only_skills(monkeypatch) -> None:
    install_fake_facade(monkeypatch)
    app = build_test_app(
        monkeypatch,
        enabled=True,
        auth_enabled=True,
        read_token="read-secret",
        operator_token="operator-secret",
    )

    runbook_payload = {
        "message": {
            "messageId": "msg-read",
            "role": "ROLE_USER",
            "parts": [
                {
                    "data": {
                        "skill": "answer_runbook_question",
                        "question": "Redis maxclients 告警如何排查？",
                    }
                }
            ],
        }
    }
    diagnosis_payload = {
        "message": {
            "messageId": "msg-diagnose-auth",
            "role": "ROLE_USER",
            "parts": [
                {
                    "data": {
                        "skill": "diagnose_incident",
                        "incident": {
                            "incident_id": "inc-auth-a2a",
                            "title": "auth check",
                            "symptom": "5xx spike",
                        },
                    }
                }
            ],
        }
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        read_only = await client.post(
            "/a2a/v1/message:send",
            headers={"X-AutoOnCall-Token": "read-secret"},
            json=runbook_payload,
        )
        read_token_diagnosis = await client.post(
            "/a2a/v1/message:send",
            headers={"X-AutoOnCall-Token": "read-secret"},
            json=diagnosis_payload,
        )
        operator_diagnosis = await client.post(
            "/a2a/v1/message:send",
            headers={"Authorization": "Bearer operator-secret"},
            json=diagnosis_payload,
        )

    assert read_only.status_code == 200
    assert read_only.json()["task"]["metadata"]["skill"] == "answer_runbook_question"
    assert read_token_diagnosis.status_code == 403
    assert operator_diagnosis.status_code == 200
    assert operator_diagnosis.json()["task"]["metadata"]["skill"] == "diagnose_incident"


@pytest.mark.asyncio
async def test_a2a_message_send_rejects_unknown_skill(monkeypatch) -> None:
    install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:send",
            json={
                "message": {
                    "messageId": "msg-unknown",
                    "role": "ROLE_USER",
                    "parts": [{"data": {"skill": "query_redis_status"}}],
                }
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "请求状态不满足当前操作，请刷新后重试"
    assert "Unsupported A2A skill" not in response.json()["detail"]


@pytest.mark.asyncio
async def test_a2a_get_task_hides_internal_lookup_error(monkeypatch) -> None:
    install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/a2a/v1/tasks/a2a-task-secret-url")

    assert response.status_code == 404
    assert response.json()["detail"] == "请求的资源不存在或已过期"
    assert "a2a-task-secret-url" not in response.json()["detail"]
