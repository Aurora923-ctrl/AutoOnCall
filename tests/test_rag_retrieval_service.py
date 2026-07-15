"""Tests for structured RAG retrieval payloads."""

import pytest
from langchain_core.documents import Document

from app.config import config
from app.services import rag_retrieval_service
from app.services.lexical_index_service import LexicalIndexService
from app.services.rag_retrieval_service import (
    build_milvus_metadata_expr,
    build_targeted_lexical_queries,
    infer_retrieval_preferences,
    normalize_metadata_filter,
    rerank_retrieval_candidates,
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


class FakePlainVectorStore:
    def __init__(self, documents):
        self.documents = documents

    def similarity_search(self, query: str, k: int, **kwargs):
        return self.documents[:k]


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


def test_structured_retrieval_excludes_stale_vector_source(monkeypatch, tmp_path) -> None:
    index = LexicalIndexService(tmp_path / "lexical.json")
    source = "docs/knowledge-base/redis.md"
    index.mark_source_stale(source, "new upload failed to index")
    monkeypatch.setattr("app.services.rag_retrieval_service.lexical_index_service", index)
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
    )

    assert payload["status"] == "no_answer"
    assert payload["retrieval_results"] == []
    assert payload["rejected_results"] == []


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
    monkeypatch.setattr("app.services.rag_retrieval_service.lexical_index_service", index)
    monkeypatch.setattr(config, "rag_min_lexical_trust_score", 1.1)

    payload = retrieve_structured_knowledge(
        "Redis maxclients connection timeout",
        top_k=1,
        vector_store=FakeVectorStore([]),
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
def test_stale_registry_read_failure_filters_vector_candidate(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.rag_retrieval_service.lexical_index_service.is_source_stale",
        lambda _source: (_ for _ in ()).throw(OSError("corrupt index")),
    )

    assert (
        rag_retrieval_service.is_stale_retrieval_source(
            {"source_path": "docs/knowledge-base/redis.md"}
        )
        is True
    )
