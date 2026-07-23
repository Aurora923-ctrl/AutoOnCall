"""Shared streaming/non-streaming RAG generation gate tests."""

from app.services import rag_answer_coverage, rag_generation_guard
from app.services.rag_answer_policy import build_extractive_grounded_answer
from app.services.rag_generation_guard import (
    finalize_grounded_answer,
    missing_required_citation_sources,
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


def test_generation_guard_reuses_frozen_evidence_for_contract_and_citations() -> None:
    preparation = prepare_grounded_generation(
        {
            "status": "success",
            "query": "Redis connected_clients 接近 maxclients 时如何判断？",
            "retrieval_results": [
                {
                    "source_file": "official_redis_clients.md",
                    "chunk_id": "official_redis_clients.md#0001",
                    "content": (
                        "Check connected_clients, maxclients, effective_capacity, and "
                        "blocked_clients."
                    ),
                }
            ],
        }
    )

    assert preparation.frozen_evidence is not None
    assert preparation.answer_contract is not None
    assert preparation.generation_payload is not None
    assert preparation.generation_payload["_frozen_generation_evidence"] is (
        preparation.frozen_evidence
    )
    assert preparation.generation_payload["_answer_contract"] is preparation.answer_contract
    assert preparation.generation_payload["retrieval_results"] == list(
        preparation.frozen_evidence.items
    )
    assert [item["citation_index"] for item in preparation.citations] == [1]


def test_generation_guard_builds_question_plan_once(monkeypatch) -> None:
    from app.services.rag_question_plan import build_question_plan

    calls = 0

    def counted_build_question_plan(query: str):
        nonlocal calls
        calls += 1
        return build_question_plan(query)

    monkeypatch.setattr(
        rag_generation_guard,
        "build_question_plan",
        counted_build_question_plan,
    )
    monkeypatch.setattr(
        rag_answer_coverage,
        "build_question_plan",
        counted_build_question_plan,
    )

    preparation = prepare_grounded_generation(
        {
            "status": "success",
            "query": "Redis connected_clients 接近 maxclients 时如何判断？",
            "retrieval_results": [
                {
                    "source_file": "redis.md",
                    "chunk_id": "redis.md#0001",
                    "content": "检查 connected_clients 和 maxclients 判断容量。",
                }
            ],
        }
    )

    assert preparation.refused is False
    assert calls == 1


def test_generation_guard_keeps_partial_evidence_and_recomputes_answer_coverage() -> None:
    preparation = prepare_grounded_generation(
        {
            "status": "success",
            "query": "如何判断 Redis 原因，并说明回滚边界？",
            "retrieval_results": [
                {
                    "source_file": "redis.md",
                    "chunk_id": "redis.md#0001",
                    "heading_path": "原因判别",
                    "content": "根据 connected_clients 判断连接耗尽。",
                }
            ],
        }
    )

    assert preparation.refused is False
    assert preparation.generation_payload is not None
    coverage = preparation.generation_payload["answer_coverage"]
    assert coverage["complete"] is False
    assert coverage["uncovered_subgoals"] == ["boundary"]


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


def test_generation_guard_rejects_topic_drift_before_citation_acceptance() -> None:
    retrieval_payload = {
        "status": "success",
        "query": "payment-service 的 pool_waiting 如何排查慢查询？",
        "answer_policy": "answer_with_citations",
        "retrieval_results": [
            {
                "source_file": "payment_wiki.html",
                "chunk_id": "payment_wiki.html#0001",
                "content": "检查 pool_waiting、active_connections 和慢查询。",
            }
        ],
    }
    citations = [
        {
            "citation_index": 1,
            "source_file": "payment_wiki.html",
            "chunk_id": "payment_wiki.html#0001",
        }
    ]

    decision = finalize_grounded_answer(
        "先生成变更计划，审批后执行。[证据 1]",
        citations,
        retrieval_payload,
        {"status": "success"},
    )

    assert decision.no_answer is True
    assert decision.citations == []


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


def test_generation_guard_repairs_missing_citation_on_grounded_claim() -> None:
    retrieval_payload = {
        "status": "success",
        "query": "如何判断 Redis connected_clients 接近 maxclients？",
        "answer_policy": "answer_with_citations",
        "retrieval_results": [
            {
                "source_file": "redis.md",
                "chunk_id": "redis.md#0001",
                "content": "检查 connected_clients 和 maxclients 判断连接容量。",
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
        "检查 connected_clients 和 maxclients 判断连接容量。",
        citations,
        retrieval_payload,
        {"status": "success"},
    )

    assert decision.no_answer is False
    assert "[证据 1]" in decision.answer
    assert decision.citations == citations


def test_generation_guard_does_not_repair_unrelated_uncited_claim() -> None:
    retrieval_payload = {
        "status": "success",
        "query": "如何判断 Redis connected_clients 接近 maxclients？",
        "answer_policy": "answer_with_citations",
        "retrieval_results": [
            {
                "source_file": "redis.md",
                "chunk_id": "redis.md#0001",
                "content": "检查 connected_clients 和 maxclients 判断连接容量。",
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
        "删除订单表中的历史数据。",
        citations,
        retrieval_payload,
        {"status": "success"},
    )

    assert decision.no_answer is True
    assert decision.citations == []


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


def test_generation_guard_keeps_uncited_evidence_gap_with_grounded_claim() -> None:
    retrieval_payload = {
        "status": "success",
        "answer_policy": "answer_with_citations",
        "retrieval_results": [
            {
                "source_file": "redis.md",
                "chunk_id": "redis.md#0001",
                "content": "检查 connected_clients 和 maxclients。",
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
        "检查 connected_clients 和 maxclients。[证据 1]\n"
        "当前证据不足：缺少当前事件的审批和回滚记录。[证据 1]",
        citations,
        retrieval_payload,
        {"status": "success"},
    )

    assert decision.no_answer is False
    assert decision.citations == citations
    assert decision.answer.endswith("当前证据不足：缺少当前事件的审批和回滚记录。")


def test_missing_required_citation_sources_uses_source_basenames() -> None:
    missing = missing_required_citation_sources(
        [
            {
                "source_file": "docs/knowledge-base/official_redis_clients.md",
                "chunk_id": "official_redis_clients.md#0001",
            }
        ],
        ["official_redis_clients.md", "redis_postmortem.pdf"],
    )

    assert missing == ["redis_postmortem.pdf"]


def test_extractive_grounded_answer_covers_required_sources() -> None:
    answer = build_extractive_grounded_answer(
        "Redis connected_clients 接近 maxclients 时如何判断？",
        [
            {
                "citation_index": 1,
                "source_file": "official_redis_clients.md",
                "chunk_id": "official_redis_clients.md#1",
                "content": "检查 connected_clients 和 maxclients，计算有效连接容量。",
            },
            {
                "citation_index": 2,
                "source_file": "redis_postmortem.pdf",
                "chunk_id": "redis_postmortem.pdf#1",
                "content": "历史复盘显示 retry amplification 推高了连接需求。",
            },
        ],
        required_sources=["official_redis_clients.md", "redis_postmortem.pdf"],
    )

    assert "[证据 1]" in answer
    assert "[证据 2]" in answer
