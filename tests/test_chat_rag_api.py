"""Chat API contract tests for explicit RAG citations."""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import chat as chat_api
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
