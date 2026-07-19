"""Tests for the business-scoped A2A facade."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api import a2a
from app.config import config
from app.models.a2a import A2ATaskRecord
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


class FakeTaskStore:
    def __init__(self) -> None:
        self.records: dict[str, A2ATaskRecord] = {}

    def create_a2a_task_record(self, record: A2ATaskRecord) -> bool:
        if record.task_id in self.records:
            return False
        self.records[record.task_id] = record
        return True

    def save_a2a_task_record(self, record: A2ATaskRecord) -> None:
        self.records[record.task_id] = record

    def get_a2a_task_record(self, task_id: str) -> A2ATaskRecord | None:
        return self.records.get(task_id)

    def list_a2a_task_records(
        self,
        *,
        incident_id: str | None = None,
        limit: int = 20,
        owner_id: str = "",
    ) -> list[A2ATaskRecord]:
        records = sorted(
            self.records.values(),
            key=lambda record: (record.updated_at, record.task_id),
            reverse=True,
        )
        if incident_id is not None:
            records = [record for record in records if record.incident_id == incident_id]
        if owner_id:
            records = [record for record in records if record.owner_id == owner_id]
        return records[:limit]


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
    auth_enabled: bool = True,
    read_token: str = "read-secret-token",
    operator_token: str = "operator-secret-token",
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
) -> tuple[FakeAIOpsService, FakeTraceService, FakeTaskStore]:
    from app.services.a2a_facade import A2AFacade

    fake_aiops = FakeAIOpsService()
    fake_trace = FakeTraceService()
    fake_task_store = FakeTaskStore()
    facade = A2AFacade(
        aiops_service=fake_aiops,
        trace_service=fake_trace,
        approval_service=FakeApprovalService(),
        report_generator=FakeReportGenerator(),
        change_execution_service=FakeChangeExecutionService(),
        rag_agent_service=FakeRagAgentService(),
        incident_state_store=FakeIncidentStateStore(),
        task_store=fake_task_store,
    )
    monkeypatch.setattr(a2a, "a2a_facade", facade)
    return fake_aiops, fake_trace, fake_task_store


def authorized_headers(scope: str = "operator") -> dict[str, str]:
    if scope == "read":
        return {"X-AutoOnCall-Token": "read-secret-token"}
    return {"Authorization": "Bearer operator-secret-token"}


def configure_named_read_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "api_read_token", "")
    monkeypatch.setattr(
        config,
        "api_auth_tokens",
        (
            '{"reader-one-secret-token":{"name":"reader-one","scopes":["read"]},'
            '"reader-two-secret-token":{"name":"reader-two","scopes":["read"]}}'
        ),
    )


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
    assert "protocolVersion" not in body
    assert body["capabilities"]["extendedAgentCard"] is True
    assert "stateTransitionHistory" not in body["capabilities"]
    assert "url" not in body
    assert "preferredTransport" not in body
    assert body["supportedInterfaces"] == [
        {
            "protocolBinding": "HTTP+JSON",
            "url": f"{config.normalized_api_base_url}/a2a/v1",
            "protocolVersion": "1.0",
        }
    ]
    assert body["capabilities"]["extensions"] == [
        {"uri": "urn:autooncall:a2a:incident-replay", "required": False},
        {"uri": "urn:autooncall:a2a:evidence-artifacts", "required": False},
    ]
    bearer = body["securitySchemes"]["bearerAuth"]["httpAuthSecurityScheme"]
    assert bearer["scheme"] == "Bearer"
    assert body["securityRequirements"] == [{"bearerAuth": []}]
    skill_ids = {item["id"] for item in body["skills"]}
    assert skill_ids == {
        "diagnose_incident",
        "get_incident_status",
        "explain_incident_replay",
        "answer_runbook_question",
    }
    assert "query_metrics" not in skill_ids
    assert "query_redis_status" not in skill_ids
    diagnosis_skill = next(item for item in body["skills"] if item["id"] == "diagnose_incident")
    assert diagnosis_skill["inputModes"] == ["application/json"]


@pytest.mark.asyncio
async def test_a2a_extended_agent_card_requires_read_scope_and_includes_examples(
    monkeypatch,
) -> None:
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        unauthorized = await client.get("/a2a/v1/extendedAgentCard")
        authorized = await client.get(
            "/a2a/v1/extendedAgentCard",
            headers=authorized_headers("read") | {"A2A-Version": "1.0"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert all(skill["examples"] for skill in authorized.json()["skills"])


@pytest.mark.asyncio
async def test_a2a_rejects_explicitly_unsupported_protocol_version(monkeypatch) -> None:
    install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers("read") | {"A2A-Version": "0.3"},
            json={
                "message": {
                    "messageId": "msg-old-version",
                    "parts": [{"data": {"question": "version check"}}],
                }
            },
        )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/a2a+json")
    assert response.json()["error"]["code"] == "unsupported_version"
    assert response.json()["error"]["details"] == [
        {"type": "UnsupportedVersion", "supportedVersions": ["1.0"]}
    ]


@pytest.mark.asyncio
async def test_a2a_business_routes_fail_closed_when_auth_is_disabled(monkeypatch) -> None:
    install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True, auth_enabled=False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:send",
            json={
                "message": {
                    "messageId": "msg-no-auth",
                    "parts": [
                        {
                            "data": {
                                "skill": "diagnose_incident",
                                "incident": {"title": "auth required", "symptom": "5xx"},
                            }
                        }
                    ],
                }
            },
        )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/a2a+json")
    assert response.json()["error"]["code"] == "authentication_required"


@pytest.mark.asyncio
async def test_a2a_message_send_runs_incident_diagnosis_as_task(monkeypatch) -> None:
    _, fake_trace, _ = install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers(),
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
    assert task["artifacts"][0]["parts"][0]["mediaType"] == "application/json"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        task_response = await client.get(
            f"/a2a/v1/tasks/{task['id']}",
            headers=authorized_headers(),
        )

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
            headers=authorized_headers("read"),
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
    assert artifact["parts"][0]["mediaType"] == "text/plain"
    assert artifact["parts"][1]["mediaType"] == "application/json"
    assert artifact["parts"][1]["data"]["citations"][0]["source_file"] == "redis.md"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {"parts": [{"data": {"question": "missing id"}}]},
        {
            "messageId": "msg-agent-role",
            "role": "ROLE_AGENT",
            "parts": [{"data": {"question": "wrong role"}}],
        },
    ],
)
async def test_a2a_message_send_rejects_invalid_message_identity(monkeypatch, message) -> None:
    install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers("read"),
            json={"message": message},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


@pytest.mark.asyncio
async def test_a2a_runbook_retry_is_idempotent_and_task_is_queryable(monkeypatch) -> None:
    _, _, task_store = install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)
    payload = {
        "message": {
            "messageId": "msg-rag-idempotent",
            "parts": [
                {
                    "data": {
                        "skill": "answer_runbook_question",
                        "question": "How should Redis saturation be investigated?",
                    }
                }
            ],
        }
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        first = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers("read"),
            json=payload,
        )
        second = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers("read"),
            json=payload,
        )
        task_id = first.json()["task"]["id"]
        queried = await client.get(
            f"/a2a/v1/tasks/{task_id}",
            headers=authorized_headers("read"),
        )

    assert first.status_code == second.status_code == queried.status_code == 200
    assert second.json()["task"] == first.json()["task"]
    assert queried.json() == first.json()["task"]
    assert list(task_store.records) == [task_id]


@pytest.mark.asyncio
async def test_a2a_task_reads_and_lists_are_scoped_to_authenticated_principal(
    monkeypatch,
) -> None:
    _, _, task_store = install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)
    configure_named_read_tokens(monkeypatch)
    payload = {
        "message": {
            "messageId": "shared-message-id",
            "parts": [
                {
                    "data": {
                        "skill": "answer_runbook_question",
                        "question": "How should Redis saturation be investigated?",
                    }
                }
            ],
        }
    }
    reader_one = {"Authorization": "Bearer reader-one-secret-token"}
    reader_two = {"Authorization": "Bearer reader-two-secret-token"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        first = await client.post(
            "/a2a/v1/message:send",
            headers=reader_one,
            json=payload,
        )
        second = await client.post(
            "/a2a/v1/message:send",
            headers=reader_two,
            json=payload,
        )
        first_task_id = first.json()["task"]["id"]
        second_task_id = second.json()["task"]["id"]
        cross_read = await client.get(
            f"/a2a/v1/tasks/{first_task_id}",
            headers=reader_two,
        )
        first_list = await client.get("/a2a/v1/tasks", headers=reader_one)
        second_list = await client.get("/a2a/v1/tasks", headers=reader_two)

    assert first.status_code == second.status_code == 200
    assert first_task_id != second_task_id
    assert cross_read.status_code == 404
    assert [item["id"] for item in first_list.json()["items"]] == [first_task_id]
    assert [item["id"] for item in second_list.json()["items"]] == [second_task_id]
    owner_ids = {record.owner_id for record in task_store.records.values()}
    assert "" not in owner_ids
    assert len(owner_ids) == 2


@pytest.mark.asyncio
async def test_a2a_status_lookup_by_incident_is_scoped_to_authenticated_principal(
    monkeypatch,
) -> None:
    install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)
    configure_named_read_tokens(monkeypatch)
    reader_two = {"Authorization": "Bearer reader-two-secret-token"}
    diagnosis_payload = {
        "message": {
            "messageId": "owner-diagnosis",
            "parts": [
                {
                    "data": {
                        "skill": "diagnose_incident",
                        "incident": {
                            "incident_id": "inc-owner-scoped",
                            "title": "owner scoped diagnosis",
                            "symptom": "5xx",
                        },
                    }
                }
            ],
        }
    }
    status_payload = {
        "message": {
            "messageId": "owner-status",
            "parts": [
                {
                    "data": {
                        "skill": "get_incident_status",
                        "incident_id": "inc-owner-scoped",
                    }
                }
            ],
        }
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        diagnosed = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers(),
            json=diagnosis_payload,
        )
        owner_status = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers(),
            json=status_payload,
        )
        other_status = await client.post(
            "/a2a/v1/message:send",
            headers=reader_two,
            json=status_payload,
        )

    assert diagnosed.status_code == owner_status.status_code == 200
    assert other_status.status_code == 404


@pytest.mark.asyncio
async def test_a2a_diagnosis_retry_is_idempotent(monkeypatch) -> None:
    fake_aiops, _, task_store = install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)
    payload = {
        "message": {
            "messageId": "msg-diagnosis-idempotent",
            "parts": [
                {
                    "data": {
                        "skill": "diagnose_incident",
                        "incident": {
                            "incident_id": "inc-idempotent",
                            "title": "repeat diagnosis",
                            "symptom": "5xx",
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
        first = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers(),
            json=payload,
        )
        second = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers(),
            json=payload,
        )

    assert first.status_code == second.status_code == 200
    assert second.json()["task"] == first.json()["task"]
    assert len(fake_aiops.snapshots) == 1
    assert list(task_store.records) == [first.json()["task"]["id"]]


@pytest.mark.asyncio
async def test_a2a_message_id_reuse_with_different_payload_is_rejected(monkeypatch) -> None:
    install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)

    def request(question: str) -> dict[str, Any]:
        return {
            "message": {
                "messageId": "msg-rag-reused",
                "parts": [
                    {
                        "data": {
                            "skill": "answer_runbook_question",
                            "question": question,
                        }
                    }
                ],
            }
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        first = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers("read"),
            json=request("Question one"),
        )
        second = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers("read"),
            json=request("Question two"),
        )

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "bad_request"


@pytest.mark.asyncio
async def test_a2a_diagnosis_ignores_client_supplied_task_id(monkeypatch) -> None:
    fake_aiops, _, _ = install_fake_facade(monkeypatch)
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
            headers=authorized_headers(),
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
        read_token="read-secret-token",
        operator_token="operator-secret-token",
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
            headers={"X-AutoOnCall-Token": "read-secret-token"},
            json=runbook_payload,
        )
        read_token_diagnosis = await client.post(
            "/a2a/v1/message:send",
            headers={"X-AutoOnCall-Token": "read-secret-token"},
            json=diagnosis_payload,
        )
        operator_diagnosis = await client.post(
            "/a2a/v1/message:send",
            headers={"Authorization": "Bearer operator-secret-token"},
            json=diagnosis_payload,
        )

    assert read_only.status_code == 200
    assert read_only.json()["task"]["metadata"]["skill"] == "answer_runbook_question"
    assert read_token_diagnosis.status_code == 403
    assert operator_diagnosis.status_code == 200
    assert operator_diagnosis.json()["task"]["metadata"]["skill"] == "diagnose_incident"


@pytest.mark.asyncio
async def test_a2a_task_namespace_hides_non_a2a_sessions(monkeypatch) -> None:
    fake_aiops, _, _ = install_fake_facade(monkeypatch)
    fake_aiops.snapshots["internal-session"] = AIOpsSessionSnapshot.from_state(
        session_id="internal-session",
        status="completed",
        node_name="workflow",
        state={
            "trace_id": "trace-internal",
            "incident": {
                "incident_id": "inc-internal",
                "title": "internal run",
                "symptom": "private",
            },
        },
    )
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        direct = await client.get(
            "/a2a/v1/tasks/internal-session",
            headers=authorized_headers("read"),
        )
        listed = await client.get(
            "/a2a/v1/tasks",
            headers=authorized_headers("read"),
        )

    assert direct.status_code == 404
    assert listed.status_code == 200
    assert listed.json() == {"items": [], "count": 0}


@pytest.mark.asyncio
async def test_a2a_status_does_not_fall_back_from_invalid_task_to_incident(monkeypatch) -> None:
    fake_aiops, _, _ = install_fake_facade(monkeypatch)
    fake_aiops.snapshots["a2a-diagnosis-valid"] = AIOpsSessionSnapshot.from_state(
        session_id="a2a-diagnosis-valid",
        status="completed",
        node_name="workflow",
        state={
            "trace_id": "trace-valid",
            "incident": {
                "incident_id": "inc-shared",
                "title": "valid A2A run",
                "symptom": "5xx",
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
            headers=authorized_headers("read"),
            json={
                "message": {
                    "messageId": "msg-status-invalid",
                    "parts": [
                        {
                            "data": {
                                "skill": "get_incident_status",
                                "task_id": "a2a-diagnosis-missing",
                                "incident_id": "inc-shared",
                            }
                        }
                    ],
                }
            },
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a2a_task_projection_does_not_publish_internal_run_model_fields(monkeypatch) -> None:
    _, _, _ = install_fake_facade(monkeypatch)
    facade = a2a.a2a_facade
    task = facade.task_from_run_status(
        {
            "session_id": "a2a-diagnosis-projection",
            "incident_id": "inc-projection",
            "trace_id": "trace-projection",
            "status": "waiting_approval",
            "status_metadata": {"label": "Waiting for approval"},
            "started_at": "2026-07-18T01:00:00+00:00",
            "updated_at": "2026-07-18T01:01:00+00:00",
            "incident": {
                "incident_id": "inc-projection",
                "title": "Projection boundary",
                "service_name": "order-service",
                "severity": "P1",
                "symptom": "5xx spike",
                "environment": "prod",
                "raw_alert": {"authorization": "Bearer secret"},
            },
            "progress": {"phase": "approval"},
            "has_report": False,
            "report_id": None,
            "final_diagnosis": "",
            "remediation_suggestion": "Review evidence",
            "errors": [],
            "warnings": [],
            "trace_summary": {"event_count": 3},
            "approval_summary": {"status": "pending"},
            "links": {"run": "/api/aiops/runs/a2a-diagnosis-projection"},
            "plan": [{"tool_name": "query_metrics"}],
            "current_plan": [{"tool_name": "query_logs"}],
            "executed_steps": [{"tool_name": "query_redis_status"}],
            "past_steps": [{"tool_name": "query_metrics"}],
            "tool_call_records": [{"input": {"token": "secret"}}],
            "pending_approval": {"metadata": {"internal_url": "http://internal"}},
            "change_plan": {"actions": ["restart"]},
            "input": "private raw prompt",
        }
    )

    run_status = task["artifacts"][0]["parts"][0]["data"]
    assert run_status["status"] == "waiting_approval"
    assert run_status["approval_summary"]["status"] == "pending"
    assert "raw_alert" not in run_status["incident"]
    for internal_field in (
        "plan",
        "current_plan",
        "executed_steps",
        "past_steps",
        "tool_call_records",
        "pending_approval",
        "change_plan",
        "input",
    ):
        assert internal_field not in run_status


@pytest.mark.asyncio
async def test_a2a_stream_emits_only_one_terminal_success_event(monkeypatch) -> None:
    install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:stream",
            headers=authorized_headers(),
            json={
                "message": {
                    "messageId": "msg-stream-single-final",
                    "role": "ROLE_USER",
                    "parts": [
                        {
                            "data": {
                                "skill": "diagnose_incident",
                                "incident": {
                                    "incident_id": "inc-stream-single-final",
                                    "title": "single final",
                                    "symptom": "5xx",
                                },
                            }
                        }
                    ],
                }
            },
        )

    assert response.status_code == 200
    assert '"final": true' not in response.text
    assert '"task": {' in response.text
    assert '"TASK_STATE_COMPLETED"' in response.text
    assert '"statusUpdate": {' not in response.text


def test_a2a_stream_payloads_wrap_status_and_artifact_updates() -> None:
    from app.services.a2a_payloads import diagnosis_event_to_a2a_event, status_update_event

    initial = status_update_event(
        task_id="a2a-diagnosis-initial",
        context_id="inc-initial",
        state="TASK_STATE_SUBMITTED",
        message="accepted",
        final=False,
        initial=True,
    )
    working = status_update_event(
        task_id="a2a-diagnosis-working",
        context_id="inc-working",
        state="TASK_STATE_WORKING",
        message="working",
        final=False,
    )
    artifact = diagnosis_event_to_a2a_event(
        task_id="a2a-diagnosis-report",
        context_id="inc-report",
        event={
            "type": "report",
            "structured_report": {
                "report_id": "rpt-stream",
                "markdown": "# report",
            },
        },
    )

    assert initial["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert working["statusUpdate"]["status"]["state"] == "TASK_STATE_WORKING"
    assert artifact["artifactUpdate"]["artifact"]["artifactId"] == "rpt-stream"
    assert artifact["artifactUpdate"]["artifact"]["parts"][0]["mediaType"] == "text/plain"
    assert "final" not in initial
    assert "final" not in working
    assert "final" not in artifact


@pytest.mark.asyncio
async def test_a2a_rejects_conflicting_context_and_incident_identity(monkeypatch) -> None:
    install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers(),
            json={
                "message": {
                    "messageId": "msg-conflicting-identity",
                    "contextId": "inc-context",
                    "parts": [
                        {
                            "data": {
                                "skill": "diagnose_incident",
                                "incident": {
                                    "incident_id": "inc-payload",
                                    "title": "identity mismatch",
                                    "symptom": "5xx",
                                },
                            }
                        }
                    ],
                }
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


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
            headers=authorized_headers(),
            json={
                "message": {
                    "messageId": "msg-unknown",
                    "role": "ROLE_USER",
                    "parts": [{"data": {"skill": "query_redis_status"}}],
                }
            },
        )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/a2a+json")
    assert response.json()["error"]["code"] == "bad_request"
    assert response.json()["error"]["message"] == "请求状态不满足当前操作，请刷新后重试"
    assert "Unsupported A2A skill" not in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_a2a_get_task_hides_internal_lookup_error(monkeypatch) -> None:
    install_fake_facade(monkeypatch)
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/a2a/v1/tasks/a2a-task-secret-url",
            headers=authorized_headers("read"),
        )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/a2a+json")
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["error"]["message"] == "请求的资源不存在或已过期"
    assert "a2a-task-secret-url" not in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_a2a_stream_converts_unexpected_exception_to_terminal_public_error(
    monkeypatch,
) -> None:
    class FailingFacade:
        def requested_skill(self, _payload):
            return "diagnose_incident"

        async def stream_message(self, _payload):
            raise RuntimeError("mysql://user:secret@internal-db unavailable")
            yield {}

    monkeypatch.setattr(a2a, "a2a_facade", FailingFacade())
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:stream",
            headers=authorized_headers(),
            json={
                "message": {
                    "messageId": "msg-stream-error",
                    "role": "ROLE_USER",
                    "parts": [{"data": {"skill": "diagnose_incident"}}],
                }
            },
        )

    assert response.status_code == 200
    assert "internal_error" in response.text
    assert '"final": true' in response.text


@pytest.mark.asyncio
async def test_a2a_sync_unexpected_exception_uses_protocol_error_media_type(monkeypatch) -> None:
    class FailingFacade:
        def requested_skill(self, _payload):
            return "answer_runbook_question"

        async def send_message(self, _payload):
            raise RuntimeError("mysql://user:secret@internal-db unavailable")

    monkeypatch.setattr(a2a, "a2a_facade", FailingFacade())
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:send",
            headers=authorized_headers("read"),
            json={"message": {"messageId": "msg-sync-error"}},
        )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/a2a+json")
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret" not in response.text
    assert "internal-db" not in response.text
    assert "secret" not in response.text
    assert "internal-db" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (LookupError("missing internal task"), "not_found"),
        (ValueError("invalid internal state"), "bad_request"),
    ],
)
async def test_a2a_stream_marks_expected_errors_as_terminal(monkeypatch, error, code) -> None:
    class FailingFacade:
        def requested_skill(self, _payload):
            return "diagnose_incident"

        async def stream_message(self, _payload):
            raise error
            yield {}

    monkeypatch.setattr(a2a, "a2a_facade", FailingFacade())
    app = build_test_app(monkeypatch, enabled=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/a2a/v1/message:stream",
            headers=authorized_headers(),
            json={
                "message": {
                    "messageId": "msg-stream-expected-error",
                    "role": "ROLE_USER",
                    "parts": [{"data": {"skill": "diagnose_incident"}}],
                }
            },
        )

    assert response.status_code == 200
    assert f'"code": "{code}"' in response.text
    assert '"final": true' in response.text
