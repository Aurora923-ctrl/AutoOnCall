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
    build_summary,
    distribution,
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
            "passed": True,
            "observability": {
                "stages": {"vector_search_ms": 10.0, "retrieval_total_ms": 12.0},
                "token_usage": {"status": "not_observed"},
            },
        },
        {
            "id": "two",
            "passed": False,
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
    assert summary["token_usage_status"] == "observed"
    failed = json.loads((tmp_path / "failed.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in failed["failed_cases"]] == ["two"]


@pytest.mark.parametrize("values", [[], [1.0]])
def test_distribution_handles_small_samples(values) -> None:
    result = distribution(values)
    assert result["count"] == len(values)
