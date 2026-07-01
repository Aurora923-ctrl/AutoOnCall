"""Tests for structured RAG retrieval payloads."""

import pytest
from langchain_core.documents import Document

from app.services.lexical_index_service import LexicalIndexService
from app.services.rag_retrieval_service import (
    build_milvus_metadata_expr,
    normalize_metadata_filter,
    retrieve_structured_knowledge,
)
from app.tools import runbook_tool as runbook_module
from app.tools.runbook_tool import SearchRunbookTool


class FakeVectorStore:
    def __init__(self, scored_documents):
        self.scored_documents = scored_documents
        self.calls = []

    def similarity_search_with_score(self, query: str, k: int, **kwargs):
        self.calls.append({"query": query, "k": k, **kwargs})
        return self.scored_documents[:k]


def test_structured_retrieval_returns_sources_scores_and_rejections() -> None:
    trusted = Document(
        page_content="Redis maxclients 耗尽会导致 connection timeout。",
        metadata={
            "_source": "aiops-docs/redis.md",
            "_file_name": "redis.md",
            "_doc_id": "aiops-docs/redis.md",
            "_chunk_id": "redis.md#0001",
            "h1": "Redis 故障",
            "h2": "maxclients",
        },
    )
    noisy = Document(
        page_content="无关内容",
        metadata={"_file_name": "noise.md", "_chunk_id": "noise.md#0001"},
    )

    payload = retrieve_structured_knowledge(
        "Redis timeout",
        top_k=2,
        max_distance=1.0,
        vector_store=FakeVectorStore([(trusted, 0.2), (noisy, 3.5)]),
    )

    assert payload["status"] == "success"
    assert len(payload["retrieval_results"]) == 1
    assert len(payload["rejected_results"]) == 1
    chunk = payload["retrieval_results"][0]
    assert chunk["doc_id"] == "aiops-docs/redis.md"
    assert chunk["source_file"] == "redis.md"
    assert chunk["heading_path"] == "Redis 故障 > maxclients"
    assert chunk["chunk_id"] == "redis.md#0001"
    assert chunk["score"] == 0.2
    assert chunk["retrieval_reason"] == "L2 distance 0.2000 小于等于 阈值 1.0000"
    assert "content_preview" in chunk
    assert payload["no_answer_rejected"] is False
    assert payload["answer_policy"] == "answer_with_citations"
    assert payload["retrieval_mode"] == "hybrid_vector_lexical_rerank"
    assert payload["candidate_k"] >= 2
    assert "引用要求" in payload["content"]
    assert "source_file: redis.md" in payload["content"]
    assert "chunk_id: redis.md#0001" in payload["content"]


def test_structured_retrieval_rejects_when_all_scores_exceed_threshold() -> None:
    document = Document(
        page_content="完全不相关的文档内容",
        metadata={"_file_name": "other.md", "_chunk_id": "other.md#0001"},
    )

    payload = retrieve_structured_knowledge(
        "Redis timeout",
        top_k=1,
        max_distance=0.5,
        vector_store=FakeVectorStore([(document, 8.0)]),
    )

    assert payload["status"] == "no_answer"
    assert payload["summary"] == "未找到可信知识来源。"
    assert payload["no_answer_rejected"] is True
    assert payload["answer_policy"] == "refuse_without_trusted_source"
    assert payload["retrieval_results"] == []
    assert len(payload["rejected_results"]) == 1
    assert payload["rejected_results"][0]["retrieval_reason"] == (
        "L2 distance 8.0000 大于 阈值 0.5000"
    )


def test_structured_retrieval_supports_metadata_filter_and_expr() -> None:
    kept = Document(
        page_content="CPU HighCPUUsage runbook",
        metadata={
            "_file_name": "cpu.md",
            "_chunk_id": "cpu.md#0001",
            "_document_version": "v2",
            "service": "billing",
        },
    )
    filtered = Document(
        page_content="Redis timeout runbook",
        metadata={
            "_file_name": "redis.md",
            "_chunk_id": "redis.md#0001",
            "_document_version": "v1",
            "service": "order",
        },
    )
    store = FakeVectorStore([(kept, 0.2), (filtered, 0.1)])

    payload = retrieve_structured_knowledge(
        "CPU billing",
        top_k=2,
        max_distance=1.0,
        metadata_filter={"_document_version": "v2", "service": "billing"},
        vector_store=store,
    )

    assert payload["status"] == "success"
    assert payload["metadata_filter"] == {"_document_version": "v2", "service": "billing"}
    assert payload["metadata_filter_expr"] == (
        'metadata["_document_version"] == "v2" and metadata["service"] == "billing"'
    )
    assert store.calls[0]["expr"] == payload["metadata_filter_expr"]
    assert [item["source_file"] for item in payload["retrieval_results"]] == ["cpu.md"]


def test_metadata_filter_rejects_unsafe_keys() -> None:
    metadata_filter = {
        "_document_version": "v2",
        'service"] == "billing" or metadata["service': "order",
        "service-name": "billing",
        "": "ignored",
    }

    normalized = normalize_metadata_filter(metadata_filter)
    expr = build_milvus_metadata_expr(metadata_filter)

    assert normalized == {"_document_version": "v2"}
    assert expr == 'metadata["_document_version"] == "v2"'
    assert "billing" not in expr
    assert "service-name" not in expr


def test_hybrid_rerank_can_promote_lexically_strong_candidate() -> None:
    weak_vector = Document(
        page_content="普通服务说明，没有具体故障处理词。",
        metadata={"_file_name": "generic.md", "_chunk_id": "generic.md#0001"},
    )
    strong_lexical = Document(
        page_content="Redis maxclients 耗尽会导致 connection timeout，需要检查连接数。",
        metadata={"_file_name": "redis.md", "_chunk_id": "redis.md#0001"},
    )

    payload = retrieve_structured_knowledge(
        "Redis maxclients connection timeout",
        top_k=1,
        max_distance=1.0,
        vector_store=FakeVectorStore([(weak_vector, 0.1), (strong_lexical, 0.25)]),
    )

    assert payload["status"] == "success"
    top = payload["retrieval_results"][0]
    assert top["source_file"] == "redis.md"
    assert top["lexical_score"] > 0
    assert top["rerank_score"] > 0


def test_hybrid_search_can_recall_lexical_only_candidate(monkeypatch, tmp_path) -> None:
    index = LexicalIndexService(tmp_path / "lexical.json")
    document = Document(
        page_content="Redis maxclients 耗尽会导致 connection timeout，需要扩容连接数。",
        metadata={
            "_source": "aiops-docs/redis.md",
            "_file_name": "redis.md",
            "_doc_id": "aiops-docs/redis.md",
            "_chunk_id": "redis.md#0001",
        },
    )
    index.upsert_source("aiops-docs/redis.md", [document])
    monkeypatch.setattr("app.services.rag_retrieval_service.lexical_index_service", index)

    payload = retrieve_structured_knowledge(
        "Redis maxclients connection timeout",
        top_k=1,
        max_distance=1.0,
        vector_store=FakeVectorStore([]),
    )

    assert payload["status"] == "success"
    assert payload["vector_candidate_count"] == 0
    assert payload["lexical_candidate_count"] == 1
    assert payload["retrieval_results"][0]["source_file"] == "redis.md"
    assert payload["retrieval_results"][0]["metadata"]["_retrieval_source"] == "lexical"


@pytest.mark.asyncio
async def test_search_runbook_tool_returns_structured_retrieval_payload(monkeypatch) -> None:
    def fake_retrieve(query: str, *, top_k=None):
        return {
            "status": "success",
            "query": query,
            "retrieval_results": [
                {
                    "source_file": "cpu_high_usage.md",
                    "chunk_id": "cpu_high_usage.md#0001",
                    "heading_path": "CPU使用率过高告警处理方案",
                    "score": 0.2,
                    "content_preview": "CPU 使用率过高处理方案",
                }
            ],
            "rejected_results": [],
            "summary": "检索到 1 条可信知识来源",
            "content": "CPU 使用率过高处理方案",
        }

    monkeypatch.setattr(runbook_module, "retrieve_structured_knowledge", fake_retrieve)
    tool = SearchRunbookTool()

    result = await tool.arun({"query": "CPU 使用率过高", "top_k": 1})

    assert result.status == "success"
    assert result.output["status"] == "success"
    assert result.output["retrieval_results"][0]["source_file"] == "cpu_high_usage.md"
    assert result.output["summary"] == "Runbook 检索命中 1 条可信片段，来源：cpu_high_usage.md"
