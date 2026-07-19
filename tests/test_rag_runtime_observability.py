"""Tests for P2-Lite RAG runtime observations and benchmark artifacts."""

from __future__ import annotations

import json

import pytest
from langchain_core.documents import Document

from app.services.rag_agent_service import (
    build_rag_observability,
    extract_message_token_usage,
)
from app.services.rag_retrieval_service import retrieve_structured_knowledge
from app.services.vector_embedding_service import DashScopeEmbeddings
from scripts.performance.run_rag_runtime_benchmark import (
    _has_valid_citation,
    build_summary,
    distribution,
    load_benchmark_cases,
    parse_args,
    runtime_execution_identity,
    select_generated_case_ids,
    write_artifacts,
)


class FakeVectorStore:
    def similarity_search_with_score(self, query: str, k: int):
        return [
            (
                Document(
                    page_content="Redis maxclients connection timeout runbook",
                    metadata={
                        "_file_name": "redis.md",
                        "_chunk_id": "redis.md#0001",
                    },
                ),
                0.1,
            )
        ]


def test_retrieval_payload_exposes_honest_stage_observations() -> None:
    payload = retrieve_structured_knowledge(
        "Redis maxclients",
        top_k=1,
        vector_store=FakeVectorStore(),
    )

    observation = payload["observability"]
    assert observation["stages"]["vector_search_ms"] >= 0
    assert observation["stages"]["embedding_ms"] == "not_observed"
    assert observation["stages"]["milvus_search_ms"] == "not_observed"
    assert observation["counts"]["trusted_count"] == 1
    assert observation["runtime"]["embedding_model"]


def test_retrieval_observability_distinguishes_hits_from_unique_candidates() -> None:
    shared = Document(
        page_content="Redis maxclients connection timeout runbook",
        metadata={
            "_doc_id": "redis.md",
            "_file_name": "redis.md",
            "_chunk_id": "redis.md#0001",
        },
    )

    class DuplicateVectorStore:
        def similarity_search_with_score(self, query: str, k: int):
            return [(shared, 0.1)]

    class DuplicateLexicalIndex:
        def search(self, query: str, *, top_k: int, metadata_filter=None):
            return [(shared, 2.0)]

        def is_source_stale(self, source_path: str) -> bool:
            return False

    payload = retrieve_structured_knowledge(
        "Redis maxclients",
        top_k=1,
        vector_store_provider=lambda: DuplicateVectorStore(),
        lexical_index=DuplicateLexicalIndex(),
        rerank_enabled=False,
    )

    counts = payload["observability"]["counts"]
    assert counts["retriever_hit_count"] == 2
    assert counts["candidate_count"] == 1
    assert counts["merged_candidate_count"] == 1
    assert counts["deduplicated_count"] == 1
    assert payload["observability"]["runtime"]["reranker_model"] == "disabled"


def test_message_usage_supports_langchain_and_openai_aliases() -> None:
    class Message:
        usage_metadata = {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
        response_metadata = {}

    assert extract_message_token_usage(Message()) == {
        "status": "observed",
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
    }


def test_default_embedding_batch_size_respects_dashscope_runtime_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.vector_embedding_service.config.dashscope_embedding_batch_size", 10
    )
    service = DashScopeEmbeddings(api_key="test-key")

    assert [len(batch) for batch in service._embedding_batches(["x"] * 23)] == [10, 10, 3]


def test_rag_observability_keeps_cost_unobserved_without_price_snapshot() -> None:
    payload = build_rag_observability(
        {
            "observability": {
                "stages": {"retrieval_total_ms": 5.0},
                "counts": {"trusted_count": 1},
                "runtime": {"retrieval_mode": "vector"},
            }
        },
        {
            "llm_generation_ms": 12.0,
            "llm_ttft_ms": "not_observed",
            "token_usage": {"status": "observed", "total_tokens": 20},
            "model": "qwen-max",
        },
        total_ms=17.0,
    )

    assert payload["stages"]["total_ms"] == 17.0
    assert payload["token_usage"]["total_tokens"] == 20
    assert payload["estimated_cost"]["status"] == "not_observed"


def test_runtime_benchmark_summary_and_artifacts(tmp_path) -> None:
    results = [
        {
            "id": "one",
            "generated": False,
            "passed": False,
            "retrieval_passed": True,
            "generation_passed": None,
            "observability": {
                "stages": {"vector_search_ms": 10.0, "retrieval_total_ms": 12.0},
                "token_usage": {"status": "not_observed"},
            },
        },
        {
            "id": "two",
            "generated": True,
            "passed": False,
            "retrieval_passed": True,
            "generation_passed": False,
            "observability": {
                "stages": {"vector_search_ms": 20.0, "retrieval_total_ms": 25.0},
                "token_usage": {"status": "observed"},
            },
        },
    ]
    summary = build_summary(results)
    payload = {
        "run": {"case_set_sha256": "abc"},
        "summary": summary,
        "cases": results,
    }

    write_artifacts(
        payload,
        json_path=tmp_path / "summary.json",
        markdown_path=tmp_path / "summary.md",
        failed_path=tmp_path / "failed.json",
    )

    assert distribution([10.0, 20.0]) == {"count": 2, "p50": 15.0, "p95": 20.0}
    assert summary["status"] == "failed"
    assert summary["retrieval"] == {
        "status": "passed",
        "passed_count": 2,
        "case_count": 2,
        "failed_cases": [],
    }
    assert summary["generation"] == {
        "status": "failed",
        "passed_count": 0,
        "case_count": 1,
        "scope": "selected_product_end_to_end_cases_including_refusals",
        "failed_cases": ["two"],
    }
    assert summary["token_usage_status"] == "observed"
    failed = json.loads((tmp_path / "failed.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in failed["retrieval_failed_cases"]] == []
    assert [item["id"] for item in failed["generation_failed_cases"]] == ["two"]
    assert summary["failed_cases"] == ["two"]


def test_runtime_generation_requires_citation_from_retrieved_sources() -> None:
    assert _has_valid_citation(
        [{"source_file": "redis.md", "chunk_id": "redis.md#0001"}],
        ["redis.md"],
        required_sources=["redis.md"],
    )
    assert not _has_valid_citation(
        [{"source_file": "other.md", "chunk_id": "other.md#0001"}],
        ["redis.md"],
        required_sources=["redis.md"],
    )


def test_runtime_generation_subset_is_independent_of_case_order() -> None:
    cases = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]

    assert select_generated_case_ids(cases, 2) == select_generated_case_ids(
        list(reversed(cases)),
        2,
    )


def test_runtime_retrieval_only_summary_does_not_claim_generation() -> None:
    summary = build_summary(
        [
            {
                "id": "one",
                "generated": False,
                "passed": False,
                "retrieval_passed": True,
                "generation_passed": None,
                "observability": {},
            }
        ]
    )

    assert summary["status"] == "retrieval_only_passed"
    assert summary["passed_count"] == 0
    assert summary["retrieval"]["status"] == "passed"
    assert summary["generation"]["status"] == "not_run"


def test_runtime_execution_identity_uses_observed_models() -> None:
    identity = runtime_execution_identity(
        [
            {
                "id": "one",
                "generated": True,
                "observability": {
                    "runtime": {
                        "llm_model": "qwen-max",
                        "embedding_model": "text-embedding-v4",
                    }
                },
            }
        ]
    )

    assert identity["actual_model"] == "qwen-max"
    assert identity["actual_embedding_model"] == "text-embedding-v4"
    assert identity["execution_path"] == "runtime_retrieval_and_generation"


def test_runtime_benchmark_rejects_empty_case_set(tmp_path) -> None:
    cases_path = tmp_path / "empty.yaml"
    cases_path.write_text("cases: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="No runtime RAG benchmark cases"):
        load_benchmark_cases(cases_path, limit=20)


def test_runtime_benchmark_defaults_to_complete_dataset(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["run_rag_runtime_benchmark.py"])

    args = parse_args()

    assert args.limit == 0
    assert args.generate_limit == 0


def test_runtime_benchmark_limit_zero_selects_every_case(tmp_path) -> None:
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: one
    query: first
    expected_source: one.md
  - id: two
    query: second
    expected_source: two.md
""",
        encoding="utf-8",
    )

    assert [item["id"] for item in load_benchmark_cases(cases_path)] == ["one", "two"]


@pytest.mark.parametrize("values", [[], [1.0]])
def test_distribution_handles_small_samples(values) -> None:
    result = distribution(values)
    assert result["count"] == len(values)
