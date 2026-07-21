"""Tests for structured RAG retrieval payloads."""

import pytest
from langchain_core.documents import Document

from app.config import config
from app.services.lexical_index_service import LexicalIndexService
from app.services.rag_retrieval import backends as retrieval_backends
from app.services.rag_retrieval.backends import (
    build_targeted_lexical_queries,
    exact_entity_lexical_results,
    targeted_lexical_results,
)
from app.services.rag_retrieval.candidates import (
    deduplicate_candidates,
    disambiguate_citation_sources,
    format_retrieval_results,
    is_stale_retrieval_source,
    merge_raw_retrieval_results,
    normalize_vector_distance,
)
from app.services.rag_retrieval.fusion import rerank_retrieval_candidates
from app.services.rag_retrieval.intent import (
    extract_exact_retrieval_entities,
    infer_retrieval_preferences,
    query_has_oncall_scope,
)
from app.services.rag_retrieval.metadata import (
    build_milvus_metadata_expr,
    normalize_metadata_filter,
)
from app.services.rag_retrieval.selection import (
    enforce_source_coverage,
    is_trusted_retrieval_chunk,
    query_is_out_of_scope,
    select_required_sources,
)
from app.services.rag_retrieval_service import retrieve_structured_knowledge
from app.tools import runbook_tool as runbook_module
from app.tools.runbook_tool import SearchRunbookTool


class FakeVectorStore:
    def __init__(self, scored_documents):
        self.scored_documents = scored_documents
        self.calls = []

    def similarity_search_with_score(self, query: str, k: int, **kwargs):
        self.calls.append({"query": query, "k": k, **kwargs})
        return self.scored_documents[:k]


class FakePlainVectorStore:
    def __init__(self, documents):
        self.documents = documents

    def similarity_search(self, query: str, k: int, **kwargs):
        return self.documents[:k]


@pytest.mark.parametrize(
    "query, top_k",
    [("", 1), ("   ", 1), ("Redis", 0), ("Redis", -1), ("Redis", 101)],
)
def test_structured_retrieval_rejects_invalid_query_or_top_k_without_search(
    query: str,
    top_k: int,
) -> None:
    store = FakeVectorStore([])

    payload = retrieve_structured_knowledge(query, top_k=top_k, vector_store=store)

    assert payload["status"] == "failed"
    assert store.calls == []


def test_structured_retrieval_rejects_invalid_distance_without_search() -> None:
    store = FakeVectorStore([])

    payload = retrieve_structured_knowledge(
        "Redis",
        max_distance=float("nan"),
        vector_store=store,
    )

    assert payload["status"] == "failed"
    assert store.calls == []


def test_structured_retrieval_bounds_expanded_candidate_count(monkeypatch) -> None:
    store = FakeVectorStore([])
    monkeypatch.setattr(config, "rag_hybrid_candidate_multiplier", 20)

    payload = retrieve_structured_knowledge("Redis", top_k=100, vector_store=store)

    assert payload["candidate_k"] == 500
    assert store.calls[0]["k"] == 500


def test_structured_retrieval_returns_sources_scores_and_rejections() -> None:
    trusted = Document(
        page_content="Redis maxclients 耗尽会导致 connection timeout。",
        metadata={
            "_source": "docs/knowledge-base/redis.md",
            "_file_name": "redis.md",
            "_doc_id": "docs/knowledge-base/redis.md",
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
    assert chunk["doc_id"] == "docs/knowledge-base/redis.md"
    assert chunk["source_file"] == "redis.md"
    assert chunk["heading_path"] == "Redis 故障 > maxclients"
    assert chunk["chunk_id"] == "redis.md#0001"
    assert chunk["score"] == 0.2
    assert chunk["retrieval_reason"] == "L2 distance 0.2000 小于等于 阈值 1.0000"
    assert "content_preview" in chunk
    assert payload["no_answer_rejected"] is False
    assert payload["answer_policy"] == "answer_with_citations"
    assert payload["retrieval_mode"] == "hybrid_vector_lexical_rerank"
    assert payload["fusion_strategy"] == "weighted"
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


def test_unclassified_oncall_query_is_not_rejected_when_context_terms_overlap() -> None:
    assert not query_is_out_of_scope(
        "服务恢复后如何验证健康检查和错误率？",
        [
            {
                "source_file": "service_unavailable.md",
                "heading_path": "恢复验证",
                "content": "服务恢复后验证健康检查、核心功能与错误率。",
                "score": 0.8,
                "lexical_score": 2.0,
            }
        ],
    )


@pytest.mark.parametrize(
    "query",
    [
        "秋招简历里的项目经历应该怎么包装更好？",
        "公司差旅报销和年假申请流程是什么？",
        "前端按钮颜色和页面圆角应该怎么设计？",
        "最近股票和基金应该怎么买，帮我给一个投资组合",
        "公司会议室预订、团建预算和行政采购流程是什么？",
    ],
)
def test_non_oncall_query_is_rejected_even_when_vector_candidates_exist(query: str) -> None:
    assert not query_has_oncall_scope(query)
    assert query_is_out_of_scope(
        query,
        [
            {
                "source_file": "service_unavailable.md",
                "content": "服务恢复后验证健康检查。",
                "score": 0.8,
                "lexical_score": 1.0,
            }
        ],
    )


def test_dependency_outage_requires_service_unavailable_runbook() -> None:
    preferences = infer_retrieval_preferences("order-service 依赖 Redis 或 MQ 不可用，导致接口失败")

    assert preferences["required_sources"] == {"service_unavailable.md"}
    assert preferences["dominant_source_terms"] == {"service_unavailable"}


def test_generic_slow_endpoint_query_requires_slow_response_runbook() -> None:
    preferences = infer_retrieval_preferences(
        "payment-service 接口响应慢，数据库慢查询数量增加，连接池等待"
    )

    assert "slow_response.md" in preferences["required_sources"]
    assert "slow_response" in preferences["dominant_source_terms"]


def test_structured_retrieval_applies_trust_gate_before_top_k_cutoff() -> None:
    untrusted = Document(
        page_content="Redis maxclients connection timeout",
        metadata={"_file_name": "untrusted.md", "_chunk_id": "untrusted#1"},
    )
    trusted = Document(
        page_content="Redis connection troubleshooting",
        metadata={"_file_name": "trusted.md", "_chunk_id": "trusted#1"},
    )

    payload = retrieve_structured_knowledge(
        "Redis maxclients connection timeout",
        top_k=1,
        max_distance=1.0,
        vector_store=FakeVectorStore([(untrusted, 8.0), (trusted, 0.8)]),
    )

    assert payload["status"] == "success"
    assert [item["source_file"] for item in payload["retrieval_results"]] == ["trusted.md"]
    assert [item["source_file"] for item in payload["rejected_results"]] == ["untrusted.md"]


def test_structured_retrieval_excludes_stale_vector_source(tmp_path) -> None:
    index = LexicalIndexService(tmp_path / "lexical.json")
    source = "docs/knowledge-base/redis.md"
    index.mark_source_stale(source, "new upload failed to index")
    document = Document(
        page_content="Redis maxclients 耗尽会导致 connection timeout。",
        metadata={
            "_source": source,
            "_file_name": "redis.md",
            "_doc_id": source,
            "_chunk_id": "redis.md#0001",
        },
    )

    payload = retrieve_structured_knowledge(
        "Redis timeout",
        top_k=1,
        max_distance=1.0,
        vector_store=FakeVectorStore([(document, 0.1)]),
        lexical_index=index,
    )

    assert payload["status"] == "no_answer"
    assert payload["retrieval_results"] == []
    assert payload["rejected_results"] == []


def test_structured_retrieval_uses_injected_lexical_index_for_stale_checks(tmp_path) -> None:
    index = LexicalIndexService(tmp_path / "lexical.json")
    source = "docs/knowledge-base/redis.md"
    index.mark_source_stale(source, "test stale registry")
    document = Document(
        page_content="Redis timeout",
        metadata={
            "_source": source,
            "_file_name": "redis.md",
            "_chunk_id": "redis.md#0001",
        },
    )

    payload = retrieve_structured_knowledge(
        "Redis timeout",
        vector_store=FakeVectorStore([(document, 0.1)]),
        lexical_index=index,
    )

    assert payload["status"] == "no_answer"
    assert payload["retrieval_results"] == []


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


def test_structured_retrieval_fails_closed_for_invalid_metadata_filter() -> None:
    document = Document(
        page_content="billing Redis timeout",
        metadata={"_file_name": "redis.md", "_chunk_id": "redis#1", "service": "billing"},
    )

    payload = retrieve_structured_knowledge(
        "Redis timeout",
        metadata_filter={"service-name": "billing"},
        vector_store=FakeVectorStore([(document, 0.1)]),
    )

    assert payload["status"] == "failed"
    assert payload["retrieval_results"] == []


def test_structured_retrieval_rejects_partially_invalid_filter_list() -> None:
    payload = retrieve_structured_knowledge(
        "Redis timeout",
        metadata_filter={"service": ["billing", {"unexpected": "value"}]},
        vector_store=FakeVectorStore([]),
    )

    assert payload["status"] == "failed"
    assert payload["retrieval_results"] == []


def test_metadata_filter_is_not_retried_without_expr_when_store_rejects_filter() -> None:
    class StoreWithoutFilterSupport:
        def __init__(self) -> None:
            self.calls = 0

        def similarity_search_with_score(self, query: str, k: int):
            self.calls += 1
            return []

    store = StoreWithoutFilterSupport()
    payload = retrieve_structured_knowledge(
        "Redis timeout",
        metadata_filter={"service": "billing"},
        vector_store=store,
    )

    assert payload["status"] == "failed"
    assert store.calls == 0


def test_metadata_post_filter_preserves_scalar_types() -> None:
    document = Document(
        page_content="Redis timeout",
        metadata={
            "_file_name": "redis.md",
            "_chunk_id": "redis.md#0001",
            "enabled": True,
        },
    )

    payload = retrieve_structured_knowledge(
        "Redis timeout",
        metadata_filter={"enabled": 1},
        vector_store=FakeVectorStore([(document, 0.1)]),
    )

    assert payload["status"] == "no_answer"


def test_hybrid_rerank_uses_observed_lexical_signal_to_promote_candidate() -> None:
    weak_vector = Document(
        page_content="普通服务说明，没有具体故障处理词。",
        metadata={"_file_name": "generic.md", "_chunk_id": "generic.md#0001"},
    )
    strong_lexical = Document(
        page_content="Redis maxclients 耗尽会导致 connection timeout，需要检查连接数。",
        metadata={
            "_file_name": "redis.md",
            "_chunk_id": "redis.md#0001",
            "_lexical_score": 4.0,
            "_lexical_rank": 1,
            "_retrieval_source": "hybrid",
        },
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


def test_rrf_strategy_can_promote_candidate_with_stronger_rank_signals() -> None:
    vector_first = {
        "rank": 1,
        "doc_id": "generic",
        "source_file": "generic.md",
        "chunk_id": "generic.md#0001",
        "score": 0.1,
        "content": "generic service notes",
        "metadata": {"_retrieval_source": "vector", "_vector_rank": 1},
    }
    hybrid_ranked = {
        "rank": 2,
        "doc_id": "redis",
        "source_file": "redis.md",
        "chunk_id": "redis.md#0001",
        "score": 0.2,
        "content": "Redis maxclients connection timeout runbook",
        "metadata": {"_retrieval_source": "hybrid", "_vector_rank": 3, "_lexical_rank": 1},
    }

    ranked = rerank_retrieval_candidates(
        "Redis maxclients connection timeout",
        [vector_first, hybrid_ranked],
        top_k=1,
        hybrid_search_enabled=True,
        rerank_enabled=True,
        fusion_strategy="rrf",
    )

    assert ranked[0]["source_file"] == "redis.md"
    assert ranked[0]["fusion_strategy"] == "rrf"
    assert ranked[0]["retrieval_signals"]["rrf_score"] == ranked[0]["rrf_score"]


def test_rrf_strategy_does_not_bypass_trust_gate() -> None:
    document = Document(
        page_content="Redis maxclients connection timeout runbook",
        metadata={"_file_name": "redis.md", "_chunk_id": "redis.md#0001"},
    )

    payload = retrieve_structured_knowledge(
        "Redis maxclients connection timeout",
        top_k=1,
        max_distance=0.5,
        vector_store=FakeVectorStore([(document, 8.0)]),
        fusion_strategy="rrf",
    )

    assert payload["status"] == "no_answer"
    assert payload["fusion_strategy"] == "rrf"
    assert payload["retrieval_results"] == []
    assert payload["rejected_results"][0]["rrf_score"] > 0


def test_rrf_counts_each_retriever_rank_once() -> None:
    ranked = rerank_retrieval_candidates(
        "Redis timeout",
        [
            {
                "doc_id": "vector",
                "source_file": "vector.md",
                "chunk_id": "vector#1",
                "score": 0.1,
                "content": "Redis timeout",
                "metadata": {"_retrieval_source": "vector", "_vector_rank": 1},
            },
            {
                "doc_id": "hybrid",
                "source_file": "hybrid.md",
                "chunk_id": "hybrid#1",
                "score": 0.2,
                "content": "Redis timeout",
                "metadata": {
                    "_retrieval_source": "hybrid",
                    "_vector_rank": 2,
                    "_lexical_rank": 1,
                },
            },
        ],
        top_k=2,
        hybrid_search_enabled=True,
        rerank_enabled=True,
        fusion_strategy="rrf",
        prune_low_relevance=False,
    )

    by_source = {item["source_file"]: item for item in ranked}
    assert by_source["vector.md"]["rrf_score"] == round(1 / 61, 4)
    assert by_source["hybrid.md"]["rrf_score"] == round((1 / 62) + (1 / 61), 4)


def test_rerank_rejects_invalid_top_k() -> None:
    with pytest.raises(ValueError, match="top_k"):
        rerank_retrieval_candidates(
            "Redis timeout",
            [],
            top_k=0,
            hybrid_search_enabled=True,
            rerank_enabled=True,
        )


def test_boolean_vector_score_is_not_treated_as_numeric_distance() -> None:
    document = Document(
        page_content="Redis timeout",
        metadata={"_file_name": "redis.md", "_chunk_id": "redis.md#0001"},
    )

    payload = retrieve_structured_knowledge(
        "Redis timeout",
        max_distance=1.0,
        vector_store=FakeVectorStore([(document, True)]),
    )

    assert payload["status"] == "no_answer"


def test_non_string_source_metadata_fails_closed() -> None:
    document = Document(
        page_content="Redis maxclients guidance",
        metadata={
            "_file_name": {"forged": "redis.md"},
            "_doc_id": ["redis.md"],
            "_chunk_id": "redis.md#0001",
        },
    )

    payload = retrieve_structured_knowledge(
        "Redis maxclients",
        max_distance=1.0,
        vector_store=FakeVectorStore([(document, 0.1)]),
    )

    assert payload["status"] == "no_answer"
    assert payload["retrieval_results"] == []


def test_duplicate_candidates_keep_the_best_distance() -> None:
    candidates = [
        {
            "doc_id": "redis",
            "chunk_id": "redis#1",
            "score": 0.8,
            "metadata": {},
        },
        {
            "doc_id": "redis",
            "chunk_id": "redis#1",
            "score": 0.2,
            "metadata": {},
        },
    ]

    deduped = deduplicate_candidates(candidates)

    assert len(deduped) == 1
    assert deduped[0]["score"] == 0.2


def test_duplicate_candidates_keep_best_signal_metadata() -> None:
    candidates = [
        {
            "doc_id": "redis",
            "source_file": "redis.md",
            "chunk_id": "redis#1",
            "score": 0.2,
            "content": "same",
            "metadata": {
                "_retrieval_source": "vector",
                "_vector_score": 0.2,
                "_vector_rank": 1,
            },
        },
        {
            "doc_id": "redis",
            "source_file": "redis.md",
            "chunk_id": "redis#1",
            "score": 0.8,
            "content": "same",
            "metadata": {
                "_retrieval_source": "lexical",
                "_lexical_score": 0.9,
                "_lexical_rank": 1,
            },
        },
    ]

    deduped = deduplicate_candidates(candidates)

    metadata = deduped[0]["metadata"]
    assert metadata["_retrieval_source"] == "hybrid"
    assert metadata["_vector_score"] == 0.2
    assert metadata["_lexical_score"] == 0.9
    assert metadata["_vector_rank"] == 1
    assert metadata["_lexical_rank"] == 1


def test_duplicate_stable_identity_with_different_content_is_rejected() -> None:
    candidates = [
        {
            "doc_id": "old-path",
            "source_file": "redis.md",
            "chunk_id": "redis.md#0001",
            "score": 0.2,
            "content": "old maxclients guidance",
            "metadata": {},
        },
        {
            "doc_id": "new-path",
            "source_file": "redis.md",
            "chunk_id": "redis.md#0001",
            "score": 0.1,
            "content": "new maxclients guidance",
            "metadata": {},
        },
    ]

    deduped = deduplicate_candidates(candidates)

    assert len(deduped) == 1
    assert deduped[0]["identity_conflict"] is True
    assert (
        is_trusted_retrieval_chunk(
            deduped[0],
            max_distance=1.0,
            min_lexical_score=0.1,
        )
        is False
    )


def test_required_source_coverage_fails_closed() -> None:
    candidates = [{"source_file": "official_redis_clients.md", "chunk_id": "official#1"}]

    selected, missing = enforce_source_coverage(
        candidates,
        required_sources={"official_redis_clients.md", "redis_postmortem.pdf"},
    )

    assert selected == []
    assert missing == {"redis_postmortem.pdf"}


def test_required_source_coverage_accepts_disambiguated_public_paths() -> None:
    candidates = [
        {
            "source_file": "uploads/official_redis_clients.md",
            "chunk_id": "official#1",
        },
        {
            "source_file": "docs/knowledge-base/redis_postmortem.pdf",
            "chunk_id": "postmortem#1",
        },
    ]

    selected, missing = enforce_source_coverage(
        candidates,
        required_sources={"official_redis_clients.md", "redis_postmortem.pdf"},
    )

    assert selected == candidates
    assert missing == set()


def test_required_source_selection_is_deterministic_and_reserves_final_budget() -> None:
    candidates = [
        {
            "doc_id": "official",
            "source_file": "official_redis_clients.md",
            "chunk_id": "official#1",
            "rerank_score": 10.0,
        },
        {
            "doc_id": "official",
            "source_file": "official_redis_clients.md",
            "chunk_id": "official#2",
            "rerank_score": 9.0,
        },
        {
            "doc_id": "postmortem",
            "source_file": "redis_postmortem.pdf",
            "chunk_id": "postmortem#1",
            "rerank_score": 1.0,
        },
    ]

    selected = select_required_sources(
        candidates,
        required_sources={"redis_postmortem.pdf", "official_redis_clients.md"},
        top_k=2,
    )[:2]

    assert [item["source_file"] for item in selected] == [
        "official_redis_clients.md",
        "redis_postmortem.pdf",
    ]


def test_required_source_selection_matches_disambiguated_public_paths() -> None:
    candidates = [
        {
            "doc_id": "official",
            "source_file": "uploads/official_redis_clients.md",
            "chunk_id": "official#1",
            "rerank_score": 10.0,
        },
        {
            "doc_id": "postmortem",
            "source_file": "docs/knowledge-base/redis_postmortem.pdf",
            "chunk_id": "postmortem#1",
            "rerank_score": 1.0,
        },
    ]

    selected = select_required_sources(
        candidates,
        required_sources={"redis_postmortem.pdf", "official_redis_clients.md"},
        top_k=2,
    )[:2]

    assert {item["chunk_id"] for item in selected} == {"official#1", "postmortem#1"}


def test_required_source_count_larger_than_top_k_cannot_claim_coverage() -> None:
    candidates = [
        {"source_file": "one.md", "chunk_id": "one#1"},
        {"source_file": "two.md", "chunk_id": "two#1"},
    ]

    selected = select_required_sources(
        candidates,
        required_sources={"one.md", "two.md"},
        top_k=1,
    )[:1]
    gated, missing = enforce_source_coverage(
        selected,
        required_sources={"one.md", "two.md"},
    )

    assert gated == []
    assert len(missing) == 1


def test_multi_source_intent_prefers_distinct_sources() -> None:
    candidates = [
        {
            "doc_id": "one",
            "source_file": "one.md",
            "chunk_id": "one#1",
            "score": 0.1,
            "content": "Redis timeout diagnosis",
            "metadata": {"_retrieval_source": "vector", "_vector_rank": 1},
        },
        {
            "doc_id": "one",
            "source_file": "one.md",
            "chunk_id": "one#2",
            "score": 0.2,
            "content": "Redis timeout verification",
            "metadata": {"_retrieval_source": "vector", "_vector_rank": 2},
        },
        {
            "doc_id": "two",
            "source_file": "two.md",
            "chunk_id": "two#1",
            "score": 0.3,
            "content": "Redis timeout boundary",
            "metadata": {"_retrieval_source": "vector", "_vector_rank": 3},
        },
    ]

    ranked = rerank_retrieval_candidates(
        "请结合多来源分别说明 Redis timeout",
        candidates,
        top_k=2,
        hybrid_search_enabled=True,
        rerank_enabled=True,
        fusion_strategy="weighted",
        prune_low_relevance=False,
    )

    assert [item["source_file"] for item in ranked] == ["one.md", "two.md"]


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), -0.1])
def test_non_finite_or_negative_vector_distance_is_not_trusted(score: float) -> None:
    document = Document(
        page_content="Redis timeout",
        metadata={"_file_name": "redis.md", "_chunk_id": "redis.md#0001"},
    )

    payload = retrieve_structured_knowledge(
        "Redis timeout",
        max_distance=1.0,
        vector_store=FakeVectorStore([(document, score)]),
    )

    assert payload["status"] == "no_answer"


def test_retrieval_result_formatter_keeps_zero_locators_and_unknown_score() -> None:
    rendered = format_retrieval_results(
        [
            {
                "rank": 1,
                "source_file": "tickets.csv",
                "chunk_id": "tickets.csv#0001",
                "score": float("nan"),
                "content": "ticket row",
                "metadata": {"page_number": 0, "row_number": 0},
            }
        ]
    )

    assert "score: 未知" in rendered
    assert "page_number: 0" in rendered
    assert "row_number: 0" in rendered


def test_merge_does_not_mark_conflicting_same_identity_as_hybrid() -> None:
    vector = Document(
        page_content="current content",
        metadata={"_doc_id": "redis", "_chunk_id": "redis#1"},
    )
    lexical = Document(
        page_content="stale content",
        metadata={"_doc_id": "redis", "_chunk_id": "redis#1"},
    )

    merged = merge_raw_retrieval_results([(vector, 0.1)], [(lexical, 2.0)])

    assert len(merged) == 1
    assert merged[0][0].page_content == "current content"
    assert merged[0][0].metadata["_retrieval_source"] == "vector"
    assert "_lexical_score" not in merged[0][0].metadata


def test_merge_uses_canonical_source_id_across_deployment_roots() -> None:
    vector = Document(
        page_content="same content",
        metadata={
            "_source": "C:/repo/docs/knowledge-base/redis.md",
            "_source_id": "docs/knowledge-base/redis.md",
            "_doc_id": "C:/repo/docs/knowledge-base/redis.md",
            "_chunk_id": "redis.md#0001",
        },
    )
    lexical = Document(
        page_content="same content",
        metadata={
            "_source": "/srv/app/docs/knowledge-base/redis.md",
            "_source_id": "docs/knowledge-base/redis.md",
            "_doc_id": "/srv/app/docs/knowledge-base/redis.md",
            "_chunk_id": "redis.md#0001",
        },
    )

    merged = merge_raw_retrieval_results([(vector, 0.1)], [(lexical, 2.0)])

    assert len(merged) == 1
    assert merged[0][0].metadata["_retrieval_source"] == "hybrid"


def test_duplicate_public_citations_are_disambiguated_by_source_identity() -> None:
    candidates = [
        {
            "doc_id": "docs/knowledge-base/team-a/runbook.md",
            "source_id": "docs/knowledge-base/team-a/runbook.md",
            "source_file": "runbook.md",
            "chunk_id": "runbook.md#0001",
        },
        {
            "doc_id": "docs/knowledge-base/team-b/runbook.md",
            "source_id": "docs/knowledge-base/team-b/runbook.md",
            "source_file": "runbook.md",
            "chunk_id": "runbook.md#0001",
        },
    ]

    result = disambiguate_citation_sources(candidates)

    assert {item["source_file"] for item in result} == {
        "docs/knowledge-base/team-a/runbook.md",
        "docs/knowledge-base/team-b/runbook.md",
    }


def test_vector_only_candidate_does_not_receive_synthetic_lexical_score() -> None:
    ranked = rerank_retrieval_candidates(
        "Redis maxclients",
        [
            {
                "doc_id": "redis",
                "source_file": "redis.md",
                "chunk_id": "redis#1",
                "score": 0.2,
                "content": "Redis maxclients",
                "metadata": {"_retrieval_source": "vector", "_vector_rank": 1},
            }
        ],
        top_k=1,
        hybrid_search_enabled=True,
        rerank_enabled=True,
    )

    assert ranked[0]["lexical_score"] == 0.0


def test_invalid_vector_score_has_zero_normalized_relevance() -> None:
    assert normalize_vector_distance("invalid") == 0.0


def test_targeted_lexical_results_respect_list_source_filter(monkeypatch) -> None:
    class RecordingIndex:
        def __init__(self) -> None:
            self.filters = []

        def search(self, query: str, *, top_k: int, metadata_filter=None):
            self.filters.append(metadata_filter)
            return []

    monkeypatch.setattr(
        retrieval_backends,
        "build_targeted_lexical_queries",
        lambda _query: {
            "allowed.md": "allowed query",
            "blocked.md": "blocked query",
        },
    )
    index = RecordingIndex()

    targeted_lexical_results(
        index,
        "query",
        metadata_filter={"_file_name": ["allowed.md", "other.md"]},
    )

    assert index.filters == [{"_file_name": "allowed.md"}]


def test_ticket_query_prefers_table_history_over_generic_runbook() -> None:
    candidates = [
        {
            "doc_id": "generic",
            "source_file": "service_unavailable.md",
            "chunk_id": "generic#1",
            "score": 0.2,
            "content": "Redis retry timeout service unavailable",
            "metadata": {"doc_type": "markdown", "_retrieval_source": "vector"},
        },
        {
            "doc_id": "ticket",
            "source_file": "tickets.csv",
            "chunk_id": "ticket#1",
            "score": 0.22,
            "content": "INC-REDIS-009 retry loop maxclients resolution",
            "metadata": {"doc_type": "table", "_retrieval_source": "vector"},
        },
    ]

    ranked = rerank_retrieval_candidates(
        "INC-REDIS-009 Redis retry history maxclients",
        candidates,
        top_k=2,
        hybrid_search_enabled=True,
        rerank_enabled=True,
    )

    assert ranked[0]["source_file"] == "tickets.csv"
    assert ranked[0]["intent_multiplier"] > 1.0


def test_rerank_does_not_pad_top_k_with_materially_weaker_context() -> None:
    candidates = [
        {
            "doc_id": "mysql",
            "source_file": "slow_response.md",
            "chunk_id": "mysql#1",
            "score": 0.1,
            "content": "MySQL slow SQL pool_waiting active_connections",
            "metadata": {"_retrieval_source": "vector"},
        },
        {
            "doc_id": "noise",
            "source_file": "cpu_high_usage.md",
            "chunk_id": "noise#1",
            "score": 1.8,
            "content": "CPU usage troubleshooting",
            "metadata": {"_retrieval_source": "vector"},
        },
    ]

    ranked = rerank_retrieval_candidates(
        "MySQL slow SQL pool_waiting",
        candidates,
        top_k=2,
        hybrid_search_enabled=True,
        rerank_enabled=True,
    )

    assert [item["source_file"] for item in ranked] == ["slow_response.md"]


def test_retrieval_preferences_only_use_explicit_source_intent() -> None:
    preferences = infer_retrieval_preferences(
        "MySQL Slow Query Postmortem payment-service active_connections"
    )

    assert preferences["preferred_doc_types"] == {"pdf"}
    assert preferences["preferred_extensions"] == {".pdf"}


def test_idle_client_query_prefers_redis_sources() -> None:
    preferences = infer_retrieval_preferences("空闲客户端连接何时会被服务端关闭？")

    assert "redis" in preferences["dominant_source_terms"]


def test_response_latency_query_prefers_slow_response_sources() -> None:
    preferences = infer_retrieval_preferences("慢 SQL 应参考哪段响应延迟原因分析？")

    assert "slow_response" in preferences["dominant_source_terms"]


def test_incident_query_prefers_ticket_history_without_requiring_one_format() -> None:
    preferences = infer_retrieval_preferences("INC-REDIS-001 Redis maxclients resolution")

    assert preferences["required_sources"] == set()
    assert ".csv" in preferences["preferred_extensions"]
    assert ".xlsx" in preferences["preferred_extensions"]
    assert preferences["prefer_ticket_history"] is True


def test_explicit_xlsx_incident_query_does_not_require_legacy_csv() -> None:
    preferences = infer_retrieval_preferences(
        "INC-REDIS-009 Redis retry history maxclients tickets.xlsx"
    )

    assert preferences["required_sources"] == {"tickets.xlsx"}
    assert ".xlsx" in preferences["preferred_extensions"]


def test_deploy_history_query_requires_xlsx_ticket_source() -> None:
    preferences = infer_retrieval_preferences("payment-service deploy_history pool_waiting tickets.xlsx")

    assert "tickets.xlsx" in preferences["required_sources"]
    assert ".xlsx" in preferences["preferred_extensions"]


def test_release_version_entity_requires_xlsx_source() -> None:
    preferences = infer_retrieval_preferences(
        "payment-api-2026.07.06-rc4 对应的部署变更是什么？"
    )

    assert "tickets.xlsx" in preferences["required_sources"]


def test_exact_entity_extraction_keeps_full_identifiers() -> None:
    assert extract_exact_retrieval_entities(
        "核对 INC-REDIS-009 与 payment-api-2026.07.06-rc4，不要命中 rc3"
    ) == {
        "inc-redis-009",
        "payment-api-2026.07.06-rc4",
        "rc3",
    }


def test_exact_entity_lexical_recall_does_not_confuse_neighboring_ids(tmp_path) -> None:
    index = LexicalIndexService(tmp_path / "lexical.json")
    index.upsert_source(
        "docs/knowledge-base/tickets.csv",
        [
            Document(
                page_content="ticket_id: INC-REDIS-001",
                metadata={
                    "_file_name": "tickets.csv",
                    "_chunk_id": "tickets.csv#0001",
                    "primary_key": "ticket_id=INC-REDIS-001",
                },
            ),
            Document(
                page_content="ticket_id: INC-REDIS-009",
                metadata={
                    "_file_name": "tickets.csv",
                    "_chunk_id": "tickets.csv#0002",
                    "primary_key": "ticket_id=INC-REDIS-009",
                },
            ),
        ],
    )

    results = exact_entity_lexical_results(
        index,
        "查询 INC-REDIS-009",
        top_k=3,
    )

    assert [item.metadata["_chunk_id"] for item, _score in results] == ["tickets.csv#0002"]


def test_source_aware_preferences_cover_capacity_wiki_and_mysql_postmortem() -> None:
    redis = infer_retrieval_preferences(
        "Redis maxclients 有哪些官方文档和容量 Wiki 可以交叉验证？"
    )
    mysql = infer_retrieval_preferences(
        "MySQL 慢查询如何联合支付 Runbook 与事故复盘取证？"
    )

    assert redis["required_sources"] == {
        "official_redis_clients.md",
        "redis_capacity_wiki.html",
    }
    assert mysql["required_sources"] == {
        "payment_wiki.html",
        "mysql_slow_query_postmortem.pdf",
    }


@pytest.mark.parametrize(
    ("query", "expected_heading"),
    [
        (
            "billing-service CPU 持续 95%，如何收集进程和线程证据并给出处置边界？",
            "升级与审批",
        ),
        (
            "Pod 发生 OOMKilled 后应如何取证，什么时候才能建议重启或扩容？",
            "相关工具命令",
        ),
        (
            "写文件失败时怎样区分磁盘空间耗尽、inode 耗尽和大目录占用？",
            "常用命令",
        ),
    ],
)
def test_runbook_query_promotes_direct_evidence_headings(
    query: str,
    expected_heading: str,
) -> None:
    candidates = [
        {
            "doc_id": "runbook",
            "source_file": "cpu_high_usage.md",
            "chunk_id": "background",
            "score": 0.2,
            "heading_path": "CPU使用率过高告警处理方案 > 问题描述",
            "content": "CPU 使用率过高会导致响应变慢。",
            "metadata": {"_retrieval_source": "vector"},
        },
        {
            "doc_id": "runbook",
            "source_file": "cpu_high_usage.md",
            "chunk_id": "direct",
            "score": 0.35,
            "heading_path": f"CPU使用率过高告警处理方案 > {expected_heading}",
            "content": "检查进程、线程、空间或 inode，并保留审批和 dry-run 边界。",
            "metadata": {"_retrieval_source": "vector"},
        },
    ]

    ranked = rerank_retrieval_candidates(
        query,
        candidates,
        top_k=2,
        hybrid_search_enabled=True,
        rerank_enabled=True,
    )

    assert ranked[0]["chunk_id"] == "direct"


@pytest.mark.parametrize(
    ("query", "required_sources"),
    [
        (
            "Redis connected_clients 接近 maxclients 时，如何结合官方限制和事故复盘判断？",
            ("official_redis_clients.md", "redis_postmortem.pdf"),
        ),
        (
            "Kubernetes 请求不通时，如何同时验证 Pod 与 Service EndpointSlice？",
            (
                "official_kubernetes_debug_pods.md",
                "official_kubernetes_debug_services.md",
            ),
        ),
        (
            "Loki 写入异常时，如何使用 discarded 指标定位并设计症状告警？",
            (
                "official_loki_troubleshoot_ingest.md",
                "official_prometheus_alerting_practices.md",
            ),
        ),
    ],
)
def test_explicit_multi_source_query_reserves_each_required_source(
    query: str,
    required_sources: tuple[str, str],
) -> None:
    candidates = [
        {
            "doc_id": "primary",
            "source_file": required_sources[0],
            "chunk_id": "primary#1",
            "score": 0.1,
            "content": query,
            "metadata": {"_retrieval_source": "vector"},
        },
        {
            "doc_id": "primary",
            "source_file": required_sources[0],
            "chunk_id": "primary#2",
            "score": 0.12,
            "content": query,
            "metadata": {"_retrieval_source": "vector"},
        },
        {
            "doc_id": "secondary",
            "source_file": required_sources[1],
            "chunk_id": "secondary#1",
            "score": 0.45,
            "content": query,
            "metadata": {"_retrieval_source": "vector"},
        },
    ]

    ranked = rerank_retrieval_candidates(
        query,
        candidates,
        top_k=2,
        hybrid_search_enabled=True,
        rerank_enabled=True,
        prune_low_relevance=False,
    )

    assert {item["source_file"] for item in ranked} == set(required_sources)


def test_structured_retrieval_keeps_trusted_required_source_after_relative_pruning() -> None:
    postmortem = Document(
        page_content="Redis incident connected_clients=9940 maxclients=10000",
        metadata={
            "_file_name": "redis_postmortem.pdf",
            "_chunk_id": "postmortem#1",
            "doc_type": "pdf",
        },
    )
    official = Document(
        page_content="Redis checks maxclients before accepting a new client connection.",
        metadata={
            "_file_name": "official_redis_clients.md",
            "_chunk_id": "official#1",
            "doc_type": "markdown",
        },
    )

    payload = retrieve_structured_knowledge(
        "Redis connected_clients 接近 maxclients 时，如何结合官方限制和事故复盘判断？",
        top_k=2,
        max_distance=1.0,
        vector_store=FakeVectorStore([(postmortem, 0.1), (official, 0.8)]),
    )

    assert {item["source_file"] for item in payload["retrieval_results"]} == {
        "official_redis_clients.md",
        "redis_postmortem.pdf",
    }


def test_targeted_lexical_queries_only_expand_explicit_subgoals() -> None:
    assert build_targeted_lexical_queries("Redis timeout") == {}

    multi_source = build_targeted_lexical_queries(
        "Loki 写入异常时，如何使用 discarded 指标定位并设计症状告警？"
    )
    assert set(multi_source) == {
        "official_loki_troubleshoot_ingest.md",
        "official_prometheus_alerting_practices.md",
    }
    assert "user-visible" in multi_source["official_prometheus_alerting_practices.md"]

    runbook = build_targeted_lexical_queries(
        "写文件失败时怎样区分磁盘空间耗尽、inode 耗尽和大目录占用？"
    )
    assert set(runbook) == {"disk_high_usage.md"}
    assert "常用命令" in runbook["disk_high_usage.md"]


def test_runtime_targeted_lexical_recall_uses_existing_filtered_index(
    monkeypatch,
    tmp_path,
) -> None:
    index = LexicalIndexService(tmp_path / "lexical.json")
    prometheus = Document(
        page_content="Alert on symptoms associated with end-user pain.",
        metadata={
            "_source": "docs/knowledge-base/official_prometheus_alerting_practices.md",
            "_file_name": "official_prometheus_alerting_practices.md",
            "_doc_id": "docs/knowledge-base/official_prometheus_alerting_practices.md",
            "_chunk_id": "prometheus#1",
        },
    )
    loki = Document(
        page_content="loki_discarded_samples_total monitors ingestion errors.",
        metadata={
            "_source": "docs/knowledge-base/official_loki_troubleshoot_ingest.md",
            "_file_name": "official_loki_troubleshoot_ingest.md",
            "_doc_id": "docs/knowledge-base/official_loki_troubleshoot_ingest.md",
            "_chunk_id": "loki#1",
        },
    )
    index.upsert_source(str(prometheus.metadata["_source"]), [prometheus])
    index.upsert_source(str(loki.metadata["_source"]), [loki])
    monkeypatch.setattr(config, "rag_min_lexical_trust_score", 0.0)

    payload = retrieve_structured_knowledge(
        "Loki 写入异常时，如何使用 discarded 指标定位并设计症状告警？",
        top_k=2,
        max_distance=1.0,
        lexical_index=index,
        vector_store_provider=lambda: FakeVectorStore([]),
    )

    assert {item["source_file"] for item in payload["retrieval_results"]} == {
        "official_loki_troubleshoot_ingest.md",
        "official_prometheus_alerting_practices.md",
    }


def test_hybrid_search_can_recall_lexical_only_candidate(monkeypatch, tmp_path) -> None:
    index = LexicalIndexService(tmp_path / "lexical.json")
    document = Document(
        page_content="Redis maxclients 耗尽会导致 connection timeout，需要扩容连接数。",
        metadata={
            "_source": "docs/knowledge-base/redis.md",
            "_file_name": "redis.md",
            "_doc_id": "docs/knowledge-base/redis.md",
            "_chunk_id": "redis.md#0001",
        },
    )
    index.upsert_source("docs/knowledge-base/redis.md", [document])
    payload = retrieve_structured_knowledge(
        "Redis maxclients connection timeout",
        top_k=1,
        max_distance=1.0,
        vector_store=FakeVectorStore([]),
        lexical_index=index,
    )

    assert payload["status"] == "success"
    assert payload["vector_candidate_count"] == 0
    assert payload["lexical_candidate_count"] == 1
    assert payload["retrieval_results"][0]["source_file"] == "redis.md"
    assert payload["retrieval_results"][0]["metadata"]["_retrieval_source"] == "lexical"
    assert payload["retrieval_results"][0]["score"] is None
    assert payload["retrieval_results"][0]["retrieval_reason"].startswith("lexical score")


def test_retrieval_degrades_to_lexical_when_default_vector_store_fails(
    monkeypatch,
    tmp_path,
) -> None:
    index = LexicalIndexService(tmp_path / "lexical.json")
    document = Document(
        page_content="Redis maxclients 耗尽会导致 connection timeout，需要扩容连接数。",
        metadata={
            "_source": "docs/knowledge-base/redis.md",
            "_file_name": "redis.md",
            "_doc_id": "docs/knowledge-base/redis.md",
            "_chunk_id": "redis.md#0001",
        },
    )
    index.upsert_source("docs/knowledge-base/redis.md", [document])

    def raise_vector_unavailable():
        raise RuntimeError("milvus unavailable")

    monkeypatch.setattr(config, "rag_hybrid_search_enabled", True)
    monkeypatch.setattr(config, "rag_rerank_enabled", True)
    monkeypatch.setattr(config, "rag_min_lexical_trust_score", 0.0)

    payload = retrieve_structured_knowledge(
        "Redis maxclients connection timeout",
        top_k=1,
        max_distance=1.0,
        lexical_index=index,
        vector_store_provider=raise_vector_unavailable,
    )

    assert payload["status"] == "success"
    assert payload["retrieval_mode"] == "lexical_degraded_rerank"
    assert payload["retrieval_degraded"] is True
    assert payload["vector_error_message"] == "向量检索暂不可用，已降级使用本地词法索引。"
    assert payload["vector_error_type"] == "RuntimeError"
    assert payload["vector_error_detail"] == "milvus unavailable"
    assert payload["vector_candidate_count"] == 0
    assert payload["lexical_candidate_count"] == 1
    assert payload["retrieval_results"][0]["source_file"] == "redis.md"
    assert payload["retrieval_results"][0]["metadata"]["_retrieval_source"] == "lexical"


def test_retrieval_degrades_to_vector_when_lexical_search_fails() -> None:
    document = Document(
        page_content="Redis maxclients timeout",
        metadata={"_file_name": "redis.md", "_chunk_id": "redis.md#0001"},
    )

    class FailingLexicalIndex:
        def search(self, *_args, **_kwargs):
            raise RuntimeError("lexical unavailable")

        def is_source_stale(self, _source_path: str) -> bool:
            return False

    payload = retrieve_structured_knowledge(
        "Redis maxclients timeout",
        max_distance=1.0,
        vector_store_provider=lambda: FakeVectorStore([(document, 0.1)]),
        lexical_index=FailingLexicalIndex(),
    )

    assert payload["status"] == "success"
    assert payload["retrieval_degraded"] is True
    assert payload["retrieval_mode"] == "vector_degraded_rerank"
    assert payload["lexical_error_type"] == "RuntimeError"
    assert payload["lexical_error_message"]
    assert [item["source_file"] for item in payload["retrieval_results"]] == ["redis.md"]


def test_trusted_results_are_reranked_after_gate_removes_higher_candidate() -> None:
    untrusted = Document(
        page_content="Redis maxclients exact timeout",
        metadata={"_file_name": "untrusted.md", "_chunk_id": "untrusted#1"},
    )
    trusted = Document(
        page_content="Redis timeout",
        metadata={"_file_name": "trusted.md", "_chunk_id": "trusted#1"},
    )

    payload = retrieve_structured_knowledge(
        "Redis maxclients exact timeout",
        top_k=2,
        max_distance=1.0,
        vector_store=FakeVectorStore([(untrusted, 8.0), (trusted, 0.5)]),
    )

    assert [item["rank"] for item in payload["retrieval_results"]] == [1]


def test_lexical_only_candidate_must_pass_lexical_trust_threshold(
    monkeypatch,
    tmp_path,
) -> None:
    index = LexicalIndexService(tmp_path / "lexical.json")
    document = Document(
        page_content="Redis maxclients connection timeout runbook.",
        metadata={
            "_source": "docs/knowledge-base/redis.md",
            "_file_name": "redis.md",
            "_doc_id": "docs/knowledge-base/redis.md",
            "_chunk_id": "redis.md#0001",
        },
    )
    index.upsert_source("docs/knowledge-base/redis.md", [document])
    monkeypatch.setattr(config, "rag_min_lexical_trust_score", 1.1)

    payload = retrieve_structured_knowledge(
        "Redis maxclients connection timeout",
        top_k=1,
        vector_store=FakeVectorStore([]),
        lexical_index=index,
    )

    assert payload["status"] == "no_answer"
    assert payload["retrieval_results"] == []
    assert payload["rejected_results"][0]["metadata"]["_retrieval_source"] == "lexical"
    assert "lexical score" in payload["rejected_results"][0]["retrieval_reason"]


def test_unscored_vector_results_are_not_trusted_by_default() -> None:
    document = Document(
        page_content="Redis maxclients 耗尽处理 runbook",
        metadata={
            "_source": "docs/knowledge-base/redis.md",
            "_file_name": "redis.md",
            "_doc_id": "docs/knowledge-base/redis.md",
            "_chunk_id": "redis.md#0001",
        },
    )

    payload = retrieve_structured_knowledge(
        "Redis maxclients",
        top_k=1,
        vector_store=FakePlainVectorStore([document]),
    )

    assert payload["status"] == "no_answer"
    assert payload["rejected_results"][0]["score"] is None
    assert payload["rejected_results"][0]["retrieval_reason"] == "检索后端未返回距离分数"


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


@pytest.mark.asyncio
async def test_search_runbook_tool_hides_vector_error_detail(monkeypatch) -> None:
    def fake_retrieve(query: str, *, top_k=None):
        return {
            "status": "no_answer",
            "query": query,
            "retrieval_degraded": True,
            "vector_error_message": "向量检索暂不可用，已降级使用本地词法索引。",
            "vector_error_type": "RuntimeError",
            "vector_error_detail": "http://milvus.internal:19530 unavailable",
            "retrieval_results": [],
            "rejected_results": [],
            "summary": "未找到可信知识来源。",
            "content": "",
        }

    monkeypatch.setattr(runbook_module, "retrieve_structured_knowledge", fake_retrieve)
    tool = SearchRunbookTool()

    result = await tool.arun({"query": "Redis timeout", "top_k": 1})

    assert result.status == "success"
    assert result.output["retrieval_degraded"] is True
    assert result.output["vector_error_message"] == "向量检索暂不可用，已降级使用本地词法索引。"
    assert "vector_error_detail" not in result.output
    assert "milvus.internal" not in str(result.output)


def test_stale_registry_read_failure_filters_vector_candidate() -> None:
    class BrokenIndex:
        def is_source_stale(self, _source: str) -> bool:
            raise OSError("corrupt index")

    assert (
        is_stale_retrieval_source(
            {"source_path": "docs/knowledge-base/redis.md"},
            lexical_index=BrokenIndex(),
        )
        is True
    )
