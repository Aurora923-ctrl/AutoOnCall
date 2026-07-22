"""Shared streaming/non-streaming RAG generation gate tests."""

from app.services.rag_generation_guard import (
    finalize_grounded_answer,
    prepare_grounded_generation,
)


def test_generation_guard_prepares_stable_citation_allowlist() -> None:
    preparation = prepare_grounded_generation(
        {
            "status": "success",
            "retrieval_results": [
                {
                    "source_file": "redis.md",
                    "chunk_id": "redis.md#0001",
                    "content": "Redis maxclients guidance",
                }
            ],
        }
    )

    assert not preparation.refused
    assert preparation.generation_payload is not None
    assert preparation.generation_payload["generation_allowlist"] == [
        {"source_file": "redis.md", "chunk_id": "redis.md#0001"}
    ]


def test_frozen_planner_accepts_generation_guard_payload_schema() -> None:
    from app.services.rag_evidence_plan import build_frozen_generation_evidence
    from app.services.rag_question_plan import build_question_plan

    plan = build_question_plan("Redis connected_clients near maxclients")
    preparation = prepare_grounded_generation(
        {
            "status": "success",
            "query": plan.query,
            "retrieval_results": [
                {
                    "source_file": "official_redis_clients.md",
                    "chunk_id": "official_redis_clients.md#0001",
                    "content": (
                        "Check connected_clients, maxclients, blocked_clients, and "
                        "effective_capacity."
                    ),
                }
            ],
        }
    )

    assert preparation.generation_payload is not None
    frozen = build_frozen_generation_evidence(plan, preparation.generation_payload)

    assert [item["chunk_id"] for item in frozen.items] == [
        "official_redis_clients.md#0001"
    ]


def test_generation_guard_finalizes_supported_answer() -> None:
    retrieval_payload = {
        "status": "success",
        "answer_policy": "answer_with_citations",
        "retrieval_results": [
            {
                "source_file": "redis.md",
                "chunk_id": "redis.md#0001",
                "content": "检查 maxclients 和 Redis 连接数。",
            }
        ],
    }
    citations = [
        {
            "citation_index": 1,
            "source_file": "redis.md",
            "chunk_id": "redis.md#0001",
        }
    ]

    decision = finalize_grounded_answer(
        "检查 maxclients。[证据 1]",
        citations,
        retrieval_payload,
        {"status": "success"},
    )

    assert not decision.no_answer
    assert decision.citations == citations
    assert decision.answer_policy == "answer_with_citations"


def test_generation_guard_rejects_citation_with_unrelated_claim() -> None:
    retrieval_payload = {
        "status": "success",
        "answer_policy": "answer_with_citations",
        "retrieval_results": [
            {
                "source_file": "redis.md",
                "chunk_id": "redis.md#0001",
                "content": "检查 maxclients 和 Redis 连接数。",
            }
        ],
    }
    citations = [
        {
            "citation_index": 1,
            "source_file": "redis.md",
            "chunk_id": "redis.md#0001",
        }
    ]

    decision = finalize_grounded_answer(
        "删除订单表中的历史数据。[证据 1]",
        citations,
        retrieval_payload,
        {"status": "success"},
    )

    assert decision.no_answer is True
    assert decision.answer_policy == "refuse_without_citation"


def test_generation_guard_checks_only_budgeted_generation_evidence() -> None:
    retrieval_payload = {
        "status": "success",
        "answer_policy": "answer_with_citations",
        "retrieval_results": [
            {
                "source_file": "redis.md",
                "chunk_id": "redis.md#0001",
                "content": "Redis tail-only claim that was removed from context.",
            }
        ],
    }
    citations = [
        {
            "citation_index": 1,
            "source_file": "redis.md",
            "chunk_id": "redis.md#0001",
        }
    ]

    decision = finalize_grounded_answer(
        "确认 tail-only claim。[证据 1]",
        citations,
        retrieval_payload,
        {"status": "success"},
        evidence=[
            {
                "source_file": "redis.md",
                "chunk_id": "redis.md#0001",
                "content": "Redis connection timeout checks.",
            }
        ],
    )

    assert decision.no_answer is True
    assert decision.answer_policy == "refuse_without_citation"


def test_generation_allowlist_malformed_payload_fails_closed() -> None:
    from app.services.rag_generation_context import build_generation_evidence

    payload = {
        "retrieval_results": [
            {
                "source_file": "redis.md",
                "chunk_id": "redis.md#0001",
                "content": "Redis connection timeout checks.",
            }
        ],
        "generation_allowlist": [{"source_file": "redis.md"}],
    }

    assert build_generation_evidence(payload) == []
