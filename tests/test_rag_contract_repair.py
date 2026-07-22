"""Orchestration tests for one contract-driven grounded-answer repair."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from app.services import rag_agent_service as rag_module


def _mysql_retrieval_payload() -> dict[str, Any]:
    return {
        "status": "success",
        "answer_policy": "answer_with_citations",
        "retrieval_results": [
            {
                "source_file": "mysql_slow_query.md",
                "chunk_id": "mysql_slow_query.md#0001",
                "content": (
                    "检查 pool_waiting、active_connections 与 slow_queries，"
                    "并对慢 SQL 执行 EXPLAIN 以排查慢查询。"
                ),
            }
        ],
    }


def _redis_retrieval_payload() -> dict[str, Any]:
    return {
        "status": "success",
        "answer_policy": "answer_with_citations",
        "required_sources": [
            "official_redis_clients.md",
            "redis_postmortem.pdf",
        ],
        "retrieval_results": [
            {
                "source_file": "official_redis_clients.md",
                "chunk_id": "official_redis_clients.md#0001",
                "content": (
                    "官方文档要求对比 connected_clients、maxclients、blocked_clients "
                    "与 effective_capacity。"
                ),
            },
            {
                "source_file": "redis_postmortem.pdf",
                "chunk_id": "redis_postmortem.pdf#0001",
                "content": (
                    "历史复盘记录 Redis 连接容量风险，不能替代当前 incident-window 证据。"
                ),
            },
        ],
    }


async def _query_with_answers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    question: str,
    retrieval_payload: dict[str, Any],
    answers: Iterator[str],
) -> tuple[dict[str, Any], list[str]]:
    service = rag_module.RagAgentService()
    prompts: list[str] = []

    async def fake_query_grounded_observed(prompt: str, *_args, **_kwargs):
        prompts.append(prompt)
        return next(answers), {"llm_generation_ms": 1.0}

    monkeypatch.setattr(service, "query_grounded_observed", fake_query_grounded_observed)
    monkeypatch.setattr(
        rag_module,
        "retrieve_structured_knowledge",
        lambda *_args, **_kwargs: {**retrieval_payload, "query": question},
    )

    result = await service.query_with_retrieval(question, "contract-repair")
    return result, prompts


@pytest.mark.asyncio
async def test_missing_mysql_entities_trigger_exactly_one_contract_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "- 检查 slow_queries。[证据 1]"
    repaired = (
        "- 对比 pool_waiting、active_connections 与 slow_queries，"
        "并对慢 SQL 执行 EXPLAIN。[证据 1]"
    )

    result, prompts = await _query_with_answers(
        monkeypatch,
        question="pool_waiting 和 active_connections 上升，如何排查慢查询？",
        retrieval_payload=_mysql_retrieval_payload(),
        answers=iter((original, repaired)),
    )

    assert len(prompts) == 2
    assert "missing_entity:EXPLAIN" in prompts[1]
    assert "allowed_evidence=1" in prompts[1]
    assert original in prompts[1]
    assert "mysql_slow_query.md#0001" in prompts[1]
    assert result["no_answer"] is False


@pytest.mark.asyncio
async def test_valid_mysql_answer_skips_semantic_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = (
        "- 对比 pool_waiting、active_connections 与 slow_queries，"
        "并对慢 SQL 执行 EXPLAIN。[证据 1]"
    )

    result, prompts = await _query_with_answers(
        monkeypatch,
        question="pool_waiting 和 active_connections 上升，如何排查慢查询？",
        retrieval_payload=_mysql_retrieval_payload(),
        answers=iter((valid,)),
    )

    assert len(prompts) == 1
    assert "slot=evidence" in prompts[0]
    assert "slot=diagnosis" in prompts[0]
    assert "required_entities=pool_waiting,active_connections,慢查询,EXPLAIN" in prompts[0]
    assert "allowed_evidence=1" in prompts[0]
    assert "max_claims=3" in prompts[0]
    assert result["no_answer"] is False


@pytest.mark.asyncio
async def test_second_invalid_redis_answer_uses_slot_aware_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = "- 对比 maxclients。[证据 1]"

    result, prompts = await _query_with_answers(
        monkeypatch,
        question=(
            "Redis connected_clients 接近 maxclients 时，"
            "如何结合官方限制和事故复盘判断？"
        ),
        retrieval_payload=_redis_retrieval_payload(),
        answers=iter((invalid, invalid)),
    )

    assert len(prompts) == 2
    assert result["no_answer"] is False
    assert "effective_capacity" in result["answer"]
    assert "历史复盘" in result["answer"]
    claim_lines = [
        line
        for line in result["answer"].split("引用来源：", 1)[0].splitlines()
        if line.strip()
    ]
    claim_indices = [
        int(re.fullmatch(r".*\[证据 (\d+)\]", line.strip()).group(1))
        for line in claim_lines
    ]
    frozen_indices = {int(item["citation_index"]) for item in result["citations"]}
    assert len(claim_indices) >= 2
    assert set(claim_indices) <= frozen_indices
