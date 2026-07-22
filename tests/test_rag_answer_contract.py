"""Claim-level answer contract tests for grounded RAG generation."""

from app.services.rag_answer_contract import (
    AnswerContract,
    AnswerSlot,
    build_answer_contract,
    validate_answer_contract,
)
from app.services.rag_evidence_plan import build_frozen_generation_evidence
from app.services.rag_question_plan import build_question_plan


def _contract(query: str, retrieval_results: list[dict[str, str]]):
    plan = build_question_plan(query)
    frozen = build_frozen_generation_evidence(
        plan,
        {
            "status": "success",
            "query": query,
            "retrieval_results": retrieval_results,
        },
    )
    return build_answer_contract(plan, frozen), [
        {
            "citation_index": item["citation_index"],
            "source_file": item["source_file"],
            "chunk_id": item["chunk_id"],
        }
        for item in frozen.items
    ]


def test_contract_reports_missing_mysql_entities() -> None:
    contract, citations = _contract(
        "payment-service 的 pool_waiting 和 active_connections 上升，如何排查慢查询？",
        [
            {
                "source_file": "mysql_slow_query.md",
                "chunk_id": "mysql_slow_query.md#0001",
                "content": (
                    "Check payment-service pool_waiting, active_connections, slow_queries, "
                    "connection hold time, and EXPLAIN output."
                ),
            }
        ],
    )

    violations = validate_answer_contract(
        "- 检查 slow_queries 和 connection hold time。[证据 1]",
        contract,
        citations,
    )

    assert {item.code for item in violations} >= {
        "missing_entity:pool_waiting",
        "missing_entity:active_connections",
        "missing_entity:EXPLAIN",
    }


def test_contract_rejects_unrequested_change_template() -> None:
    contract, citations = _contract(
        "payment-service 的 pool_waiting 为什么上升？",
        [
            {
                "source_file": "mysql_diagnosis.md",
                "chunk_id": "mysql_diagnosis.md#0001",
                "content": "Diagnose payment-service pool_waiting from connection hold time.",
            }
        ],
    )

    violations = validate_answer_contract(
        "- 变更计划包含 approver、canary、观察时长和 rollback。[证据 1]",
        contract,
        citations,
    )

    assert any(item.code == "unrequested_change_template" for item in violations)


def test_contract_requires_postmortem_claim_contribution() -> None:
    contract, citations = _contract(
        "Redis connected_clients 接近 maxclients 时，如何结合官方限制和事故复盘判断？",
        [
            {
                "source_file": "official_redis_clients.md",
                "chunk_id": "official_redis_clients.md#0001",
                "content": (
                    "Check connected_clients, maxclients, effective_capacity, and "
                    "blocked_clients."
                ),
            },
            {
                "source_file": "redis_postmortem.pdf",
                "chunk_id": "redis_postmortem.pdf#0001",
                "content": (
                    "历史事故复盘记录 connected_clients 接近 maxclients 时的连接容量风险。"
                ),
            },
        ],
    )

    violations = validate_answer_contract(
        "- 检查 effective_capacity 和 blocked_clients。[证据 1]",
        contract,
        citations,
    )

    assert any(item.code == "missing_source_role:postmortem" for item in violations)


def test_contract_rejects_two_citations_on_one_claim() -> None:
    contract, citations = _contract(
        "Redis 当前容量如何结合官方限制和历史工单判断？",
        [
            {
                "source_file": "official_redis_clients.md",
                "chunk_id": "official_redis_clients.md#0001",
                "content": "Official Redis maxclients capacity guidance.",
            },
            {
                "source_file": "tickets.xlsx",
                "chunk_id": "tickets.xlsx#0001",
                "content": "历史工单记录 Redis 连接容量事件。",
            },
        ],
    )

    violations = validate_answer_contract(
        "- 历史工单不能替代当前证据。[证据 1][证据 2]",
        contract,
        citations,
    )

    assert any(item.code == "multiple_citations_in_claim" for item in violations)


def test_contract_rejects_citation_only_line_as_empty_claim() -> None:
    contract = AnswerContract(
        slots=(AnswerSlot("evidence", (), (1,), ()),),
        max_claims=3,
    )

    violations = validate_answer_contract(
        "[证据 1]",
        contract,
        [{"citation_index": 1, "source_file": "runbook.md", "chunk_id": "chunk-1"}],
    )

    assert any(item.code == "empty_claim" for item in violations)


def test_contract_rejects_change_template_split_across_lines() -> None:
    contract = AnswerContract(
        slots=(AnswerSlot("diagnosis", (), (1,), ()),),
        max_claims=3,
    )
    citations = [
        {"citation_index": 1, "source_file": "diagnosis.md", "chunk_id": "chunk-1"}
    ]

    violations = validate_answer_contract(
        "Approver: Alice. [证据 1]\n"
        "Canary: 10%. [证据 1]\n"
        "Rollback: stop on errors. [证据 1]",
        contract,
        citations,
    )

    assert any(item.code == "unrequested_change_template" for item in violations)
