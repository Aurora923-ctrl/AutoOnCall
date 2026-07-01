"""RAG stream boundary behavior tests."""

import pytest

from app.services import rag_agent_service as rag_module


class AIMessageChunk:
    def __init__(self, content: str):
        self.content = content


class ContentOnlyAgent:
    async def astream(self, *_args, **_kwargs):
        yield AIMessageChunk("hello"), {"langgraph_node": "model"}


class RaisingAgent:
    async def astream(self, *_args, **_kwargs):
        raise RuntimeError("stream failed")
        yield  # pragma: no cover


def _create_service(monkeypatch, agent):
    monkeypatch.setattr(rag_module, "ChatQwen", lambda **_kwargs: object())
    service = rag_module.RagAgentService()

    async def initialize_agent() -> None:
        service.agent = agent
        service._agent_initialized = True

    monkeypatch.setattr(service, "_initialize_agent", initialize_agent)
    return service


@pytest.mark.asyncio
async def test_query_stream_reads_plain_content_when_content_blocks_are_absent(monkeypatch) -> None:
    service = _create_service(monkeypatch, ContentOnlyAgent())

    chunks = [chunk async for chunk in service.query_stream("hello?", "session-stream")]

    assert chunks[0] == {"type": "content", "data": "hello", "node": "model"}
    assert chunks[-1] == {"type": "complete"}


@pytest.mark.asyncio
async def test_query_stream_raises_without_yielding_duplicate_error_chunk(monkeypatch) -> None:
    service = _create_service(monkeypatch, RaisingAgent())

    chunks = []
    with pytest.raises(RuntimeError, match="stream failed"):
        async for chunk in service.query_stream("hello?", "session-error"):
            chunks.append(chunk)

    assert chunks == []
