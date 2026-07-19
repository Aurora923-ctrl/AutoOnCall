"""Chat API contract tests for explicit RAG citations."""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import chat as chat_api
from app.config import config
from app.main import app
from app.models.request import ChatRequest, ClearRequest


def test_chat_request_models_reject_unbounded_session_and_question_inputs() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(Id="s" * 129, Question="hello")
    with pytest.raises(ValidationError):
        ChatRequest(Id="session-1", Question="")
    with pytest.raises(ValidationError):
        ChatRequest(Id="session-1", Question="   ")
    with pytest.raises(ValidationError):
        ChatRequest(Id="   ", Question="hello")
    with pytest.raises(ValidationError):
        ClearRequest(sessionId="s" * 129)
    with pytest.raises(ValidationError):
        ClearRequest(sessionId="   ")
    with pytest.raises(ValidationError):
        ChatRequest(Id="session-\nforged", Question="hello")
    with pytest.raises(ValidationError):
        ClearRequest(sessionId="session-\tforged")

    assert ChatRequest(Id="中文会话", Question="hello").id == "中文会话"


@pytest.mark.asyncio
async def test_chat_returns_citations_and_retrieval_metadata(monkeypatch) -> None:
    async def fake_query_with_retrieval(
        question: str,
        session_id: str,
        metadata_filter: dict | None = None,
    ) -> dict:
        assert question == "Redis timeout 怎么处理？"
        assert session_id == "rag-session"
        assert metadata_filter is None
        return {
            "success": True,
            "answer": "检查 Redis 连接数。\n\n引用来源：\n- source_file: redis.md; chunk_id: redis.md#0001; score: 0.1200",
            "citations": [
                {
                    "source_file": "redis.md",
                    "chunk_id": "redis.md#0001",
                    "score": 0.12,
                    "content_preview": "Redis maxclients 耗尽会导致连接超时。",
                }
            ],
            "retrieval": {
                "status": "success",
                "summary": "检索到 1 条可信知识来源",
                "retrieval_results": [
                    {
                        "source_file": "redis.md",
                        "chunk_id": "redis.md#0001",
                        "score": 0.12,
                    }
                ],
                "rejected_results": [],
                "no_answer_rejected": False,
            },
            "no_answer": False,
            "answer_policy": "answer_with_citations",
            "observability": {
                "runtime": {"llm_model": "qwen-max"},
                "token_usage": {"status": "observed", "total_tokens": 42},
            },
        }

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.query_with_retrieval",
        fake_query_with_retrieval,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/chat",
            json={"Id": "rag-session", "Question": "Redis timeout 怎么处理？"},
        )

    payload = response.json()

    assert response.status_code == 200
    assert payload["data"]["success"] is True
    assert payload["data"]["citations"][0]["source_file"] == "redis.md"
    assert payload["data"]["citations"][0]["chunk_id"] == "redis.md#0001"
    assert payload["data"]["retrieval"]["status"] == "success"
    assert payload["data"]["noAnswer"] is False
    assert payload["data"]["answerPolicy"] == "answer_with_citations"
    assert payload["data"]["observability"]["runtime"]["llm_model"] == "qwen-max"


@pytest.mark.asyncio
async def test_chat_returns_http_500_when_rag_service_fails(monkeypatch) -> None:
    async def fail_query_with_retrieval(
        question: str,
        session_id: str,
        metadata_filter: dict | None = None,
    ) -> dict:
        raise RuntimeError("retrieval backend unavailable")

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.query_with_retrieval",
        fail_query_with_retrieval,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/chat",
            json={"Id": "rag-session", "Question": "Redis timeout 怎么处理？"},
        )

    payload = response.json()

    assert response.status_code == 500
    assert payload["code"] == 500
    assert payload["data"]["success"] is False
    assert payload["data"]["errorMessage"] == chat_api.PUBLIC_CHAT_ERROR_MESSAGE
    assert "retrieval backend unavailable" not in payload["data"]["errorMessage"]


@pytest.mark.asyncio
async def test_chat_stream_emits_search_results_before_done(monkeypatch) -> None:
    async def fake_query_stream_with_retrieval(
        question: str,
        session_id: str,
        metadata_filter: dict | None = None,
    ):
        assert metadata_filter == {"_document_version": "v2"}
        yield {
            "type": "search_results",
            "data": {
                "status": "no_answer",
                "summary": "未找到可信知识来源。",
                "retrieval_results": [],
                "rejected_results": [
                    {
                        "source_file": "noise.md",
                        "chunk_id": "noise.md#0001",
                        "score": 9.0,
                    }
                ],
                "no_answer_rejected": True,
            },
        }
        yield {"type": "content", "data": "未找到可信知识来源。"}
        yield {
            "type": "complete",
            "data": {
                "answer": "未找到可信知识来源。",
                "citations": [],
                "retrieval": {
                    "status": "no_answer",
                    "summary": "未找到可信知识来源。",
                    "retrieval_results": [],
                    "rejected_results": [],
                    "no_answer_rejected": True,
                },
                "no_answer": True,
                "answer_policy": "refuse_without_trusted_source",
            },
        }

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.query_stream_with_retrieval",
        fake_query_stream_with_retrieval,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/api/chat_stream",
            json={
                "Id": "rag-stream",
                "Question": "简历怎么写？",
                "metadataFilter": {"_document_version": "v2"},
            },
        ) as response:
            body = await response.aread()

    text = body.decode("utf-8")

    assert response.status_code == 200
    assert '"type": "search_results"' in text
    assert '"type": "content"' in text
    assert '"type": "done"' in text
    assert "refuse_without_trusted_source" in text


@pytest.mark.asyncio
async def test_chat_stream_returns_public_error_when_rag_service_fails(monkeypatch) -> None:
    async def fail_query_stream_with_retrieval(
        question: str,
        session_id: str,
        metadata_filter: dict | None = None,
    ):
        raise RuntimeError("stream backend unavailable")
        yield

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.query_stream_with_retrieval",
        fail_query_stream_with_retrieval,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/api/chat_stream",
            json={"Id": "rag-stream", "Question": "Redis timeout 怎么处理？"},
        ) as response:
            body = await response.aread()

    text = body.decode("utf-8")

    assert response.status_code == 200
    assert chat_api.PUBLIC_CHAT_STREAM_ERROR_MESSAGE in text
    assert "stream backend unavailable" not in text


@pytest.mark.asyncio
async def test_chat_stream_redacts_service_error_event_details(monkeypatch) -> None:
    async def error_query_stream(
        question: str,
        session_id: str,
        metadata_filter: dict | None = None,
    ):
        yield {
            "type": "error",
            "data": "provider rejected credential sk-sensitive-secret",
        }

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.query_stream_with_retrieval",
        error_query_stream,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/api/chat_stream",
            json={"Id": "rag-stream", "Question": "Redis timeout 怎么处理？"},
        ) as response:
            text = (await response.aread()).decode("utf-8")

    assert text.count('"type": "error"') == 1
    assert chat_api.PUBLIC_CHAT_STREAM_ERROR_MESSAGE in text
    assert "provider rejected credential" not in text
    assert "sk-sensitive-secret" not in text


@pytest.mark.asyncio
async def test_chat_stream_emits_only_validated_service_content(monkeypatch) -> None:
    async def fake_query_stream_with_retrieval(
        question: str,
        session_id: str,
        metadata_filter: dict | None = None,
    ):
        yield {
            "type": "search_results",
            "data": {
                "status": "success",
                "retrieval_results": [
                    {
                        "source_file": "redis.md",
                        "chunk_id": "redis.md#0001",
                    }
                ],
            },
        }
        yield {
            "type": "content",
            "data": "已通过引用门禁。[redis.md | redis.md#0001]",
        }
        yield {
            "type": "complete",
            "data": {
                "answer": "已通过引用门禁。[redis.md | redis.md#0001]",
                "citations": [
                    {
                        "source_file": "redis.md",
                        "chunk_id": "redis.md#0001",
                    }
                ],
                "retrieval": {"status": "success"},
                "no_answer": False,
                "answer_policy": "answer_with_citations",
            },
        }

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.query_stream_with_retrieval",
        fake_query_stream_with_retrieval,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/api/chat_stream",
            json={"Id": "rag-stream", "Question": "Redis timeout 怎么处理？"},
        ) as response:
            text = (await response.aread()).decode("utf-8")

    assert text.count('"type": "content"') == 1
    assert "已通过引用门禁" in text
    assert text.count('"type": "done"') == 1


@pytest.mark.asyncio
async def test_chat_stream_emits_error_when_service_ends_without_terminal_event(
    monkeypatch,
) -> None:
    async def incomplete_query_stream(
        question: str,
        session_id: str,
        metadata_filter: dict | None = None,
    ):
        yield {"type": "search_results", "data": {"status": "success"}}

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.query_stream_with_retrieval",
        incomplete_query_stream,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/api/chat_stream",
            json={"Id": "rag-stream", "Question": "Redis timeout 怎么处理？"},
        ) as response:
            text = (await response.aread()).decode("utf-8")

    assert text.count('"type": "error"') == 1
    assert '"type": "done"' not in text
    assert chat_api.PUBLIC_CHAT_STREAM_ERROR_MESSAGE in text


@pytest.mark.asyncio
async def test_chat_stream_ignores_events_after_first_terminal_event(monkeypatch) -> None:
    async def duplicate_terminal_stream(
        question: str,
        session_id: str,
        metadata_filter: dict | None = None,
    ):
        yield {
            "type": "complete",
            "data": {
                "answer": "done",
                "citations": [],
                "retrieval": {"status": "no_answer"},
                "no_answer": True,
                "answer_policy": "refuse_without_trusted_source",
            },
        }
        yield {"type": "error", "data": "late error"}

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.query_stream_with_retrieval",
        duplicate_terminal_stream,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/api/chat_stream",
            json={"Id": "rag-stream", "Question": "Redis timeout 怎么处理？"},
        ) as response:
            text = (await response.aread()).decode("utf-8")

    assert text.count('"type": "done"') == 1
    assert '"type": "error"' not in text
    assert "late error" not in text


@pytest.mark.asyncio
async def test_clear_session_awaits_async_service_cleanup(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_clear_session(session_id: str) -> bool:
        calls.append(session_id)
        return True

    monkeypatch.setattr("app.api.chat.rag_agent_service.clear_session", fake_clear_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/chat/clear", json={"sessionId": "clear-me"})

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert calls == ["clear-me"]


@pytest.mark.asyncio
async def test_clear_session_returns_http_500_when_service_cleanup_fails(monkeypatch) -> None:
    async def fake_clear_session(_session_id: str) -> bool:
        return False

    monkeypatch.setattr("app.api.chat.rag_agent_service.clear_session", fake_clear_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/chat/clear", json={"sessionId": "clear-me"})

    assert response.status_code == 500
    assert response.json()["detail"] == chat_api.PUBLIC_SESSION_ERROR_MESSAGE


@pytest.mark.asyncio
async def test_authenticated_chat_sessions_are_isolated_by_presented_token(monkeypatch) -> None:
    seen_session_ids: list[str] = []
    monkeypatch.setattr(config, "api_auth_enabled", True)
    monkeypatch.setattr(config, "api_auth_tokens", "")
    monkeypatch.setattr(config, "api_read_token", "")
    monkeypatch.setattr(config, "api_operator_token", "operator-secret-token")
    monkeypatch.setattr(config, "api_approver_token", "")
    monkeypatch.setattr(config, "api_change_token", "")
    monkeypatch.setattr(config, "api_admin_token", "admin-secret-token")

    async def fake_query_with_retrieval(
        question: str,
        session_id: str,
        metadata_filter: dict | None = None,
    ) -> dict:
        seen_session_ids.append(session_id)
        return {
            "success": True,
            "answer": question,
            "citations": [],
            "retrieval": {"status": "no_answer"},
            "no_answer": True,
            "answer_policy": "refuse_without_trusted_source",
        }

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.query_with_retrieval",
        fake_query_with_retrieval,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for token in ("operator-secret-token", "admin-secret-token"):
            response = await client.post(
                "/api/chat",
                headers={"Authorization": f"Bearer {token}"},
                json={"Id": "shared-session", "Question": "hello"},
            )
            assert response.status_code == 200

    assert len(seen_session_ids) == 2
    assert seen_session_ids[0] != seen_session_ids[1]
    assert all(item.endswith(":shared-session") for item in seen_session_ids)


@pytest.mark.asyncio
async def test_authenticated_history_and_clear_share_private_namespace(monkeypatch) -> None:
    history_calls: list[str] = []
    clear_calls: list[str] = []
    monkeypatch.setattr(config, "api_auth_enabled", True)
    monkeypatch.setattr(config, "api_auth_tokens", "")
    monkeypatch.setattr(config, "api_read_token", "")
    monkeypatch.setattr(config, "api_operator_token", "operator-secret-token")
    monkeypatch.setattr(config, "api_approver_token", "")
    monkeypatch.setattr(config, "api_change_token", "")
    monkeypatch.setattr(config, "api_admin_token", "")

    async def fake_get_session_history(session_id: str) -> list[dict]:
        history_calls.append(session_id)
        return []

    async def fake_clear_session(session_id: str) -> bool:
        clear_calls.append(session_id)
        return True

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.get_session_history",
        fake_get_session_history,
    )
    monkeypatch.setattr("app.api.chat.rag_agent_service.clear_session", fake_clear_session)

    headers = {"Authorization": "Bearer operator-secret-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        history_response = await client.get(
            "/api/chat/session/private-session",
            headers=headers,
        )
        clear_response = await client.post(
            "/api/chat/clear",
            headers=headers,
            json={"sessionId": "private-session"},
        )

    assert history_response.status_code == 200
    assert clear_response.status_code == 200
    assert history_calls == clear_calls
    assert history_calls[0].endswith(":private-session")


@pytest.mark.asyncio
async def test_get_session_info_awaits_async_history_read(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_get_session_history(session_id: str) -> list[dict[str, str]]:
        calls.append(session_id)
        return [
            {
                "role": "user",
                "content": "question",
                "timestamp": "2026-07-15T12:00:00",
            }
        ]

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.get_session_history",
        fake_get_session_history,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/chat/session/history-session")

    assert response.status_code == 200
    assert response.json()["message_count"] == 1
    assert calls == ["history-session"]


@pytest.mark.asyncio
async def test_get_session_info_returns_http_500_when_history_read_fails(monkeypatch) -> None:
    async def fail_get_session_history(session_id: str) -> list[dict[str, str]]:
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.get_session_history",
        fail_get_session_history,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/chat/session/history-session")

    assert response.status_code == 500
    assert response.json()["detail"] == chat_api.PUBLIC_SESSION_ERROR_MESSAGE
    assert "checkpoint unavailable" not in response.text


@pytest.mark.asyncio
async def test_get_session_info_preserves_rag_metadata(monkeypatch) -> None:
    async def fake_get_session_history(_session_id: str) -> list[dict]:
        return [
            {
                "role": "assistant",
                "content": "检查连接数。[redis.md | redis.md#0001]",
                "timestamp": "2026-07-18T12:00:00",
                "metadata": {
                    "citations": [{"source_file": "redis.md", "chunk_id": "redis.md#0001"}],
                    "retrieval": {"status": "success"},
                    "noAnswer": False,
                    "answerPolicy": "answer_with_citations",
                },
            }
        ]

    monkeypatch.setattr(
        "app.api.chat.rag_agent_service.get_session_history",
        fake_get_session_history,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/chat/session/history-session")

    metadata = response.json()["history"][0]["metadata"]
    assert metadata["citations"][0]["chunk_id"] == "redis.md#0001"
    assert metadata["answerPolicy"] == "answer_with_citations"
