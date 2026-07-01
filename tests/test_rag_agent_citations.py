"""Unit tests for RAG answer citation helpers."""

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
    assert compact["answer_policy"] == "refuse_without_trusted_source"
