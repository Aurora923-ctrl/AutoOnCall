"""Unit tests for RAG answer citation helpers."""

from types import SimpleNamespace

import pytest

from app.services import rag_agent_service as rag_module
from app.services.rag_agent_service import (
    build_no_answer_message,
    compact_retrieval_payload,
    ensure_citation_block,
)


def test_ensure_citation_block_appends_missing_sources() -> None:
    answer = "Redis timeout 通常需要先确认连接数。"
    citations = [
        {
            "source_file": "redis.md",
            "chunk_id": "redis.md#0001",
            "score": 0.12345,
        }
    ]

    grounded = ensure_citation_block(answer, citations)

    assert "引用来源" in grounded
    assert "source_file: redis.md" in grounded
    assert "chunk_id: redis.md#0001" in grounded
    assert "score: 0.1235" in grounded


def test_no_answer_payload_keeps_rejected_candidates_for_frontend() -> None:
    payload = {
        "status": "no_answer",
        "summary": "未找到可信知识来源。",
        "answer_policy": "refuse_without_trusted_source",
        "no_answer_rejected": True,
        "retrieval_results": [],
        "rejected_results": [
            {
                "source_file": "noise.md",
                "chunk_id": "noise.md#0001",
                "score": 8.0,
                "content_preview": "无关内容",
                "retrieval_reason": "L2 distance 8.0000 大于 阈值 0.5000",
            }
        ],
    }

    message = build_no_answer_message(payload)
    compact = compact_retrieval_payload(payload)

    assert "请补充相关知识库文档后再提问" in message
    assert compact["status"] == "no_answer"
    assert compact["no_answer_rejected"] is True
    assert compact["rejected_results"][0]["source_file"] == "noise.md"
    assert compact["rejected_results"][0]["source_path"] == "noise.md"
    assert compact["answer_policy"] == "refuse_without_trusted_source"


def test_compact_retrieval_payload_hides_absolute_source_path() -> None:
    payload = {
        "status": "success",
        "retrieval_results": [
            {
                "source_file": "redis.md",
                "source_path": "/srv/autooncall/uploads/redis.md",
                "chunk_id": "redis.md#0001",
            }
        ],
    }

    compact = compact_retrieval_payload(payload)

    assert compact["retrieval_results"][0]["source_path"] == "redis.md"


@pytest.mark.asyncio
async def test_query_with_retrieval_uses_tool_free_grounded_model(monkeypatch) -> None:
    class FakeGroundedModel:
        def __init__(self) -> None:
            self.messages = []

        async def ainvoke(self, messages):
            self.messages = messages
            return SimpleNamespace(content="根据知识库，先检查 Redis 连接数。")

    service = rag_module.RagAgentService()
    service.model = FakeGroundedModel()

    async def fail_if_agent_initializes() -> None:
        raise AssertionError("grounded RAG answer must not initialize Agent tools")

    monkeypatch.setattr(service, "_initialize_agent", fail_if_agent_initializes)
    monkeypatch.setattr(
        rag_module,
        "retrieve_structured_knowledge",
        lambda *_args, **_kwargs: {
            "status": "success",
            "content": "source_file: redis.md\nchunk_id: redis.md#0001\nRedis 连接数过高会导致超时。",
            "summary": "检索到 1 条可信知识来源",
            "retrieval_results": [
                {
                    "source_file": "redis.md",
                    "chunk_id": "redis.md#0001",
                    "score": 0.12,
                    "content_preview": "Redis 连接数过高会导致超时。",
                }
            ],
            "rejected_results": [],
            "answer_policy": "answer_with_citations",
        },
    )

    result = await service.query_with_retrieval("Redis timeout 怎么处理？", "session-grounded")

    assert result["no_answer"] is False
    assert "引用来源" in result["answer"]
    assert "redis.md#0001" in result["answer"]
    assert service.model.messages
    history = service.get_session_history("session-grounded")
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[0]["content"] == "Redis timeout 怎么处理？"
    assert "redis.md#0001" in history[1]["content"]
