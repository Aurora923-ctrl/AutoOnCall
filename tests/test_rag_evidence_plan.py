"""Tests for immutable generation-time evidence planning."""

from app.services import rag_evidence_plan
from app.services.rag_evidence_plan import (
    build_frozen_generation_evidence,
    classify_source_role,
)
from app.services.rag_generation_context import format_frozen_generation_context
from app.services.rag_question_plan import build_question_plan


def test_frozen_evidence_binds_official_and_postmortem_roles() -> None:
    plan = build_question_plan(
        "Redis connected_clients 接近 maxclients 时，如何结合官方限制和事故复盘判断？"
    )
    payload = {
        "status": "success",
        "query": plan.query,
        "retrieval_results": [
            {
                "rank": 1,
                "source_file": "official_redis_clients.md",
                "chunk_id": "official_redis_clients.md#capacity",
                "heading_path": "Redis 客户端连接限制",
                "content": (
                    "检查 connected_clients 是否接近 maxclients；"
                    "effective_capacity = maxclients - reserved_connections，"
                    "并同时观察 blocked_clients。"
                ),
            },
            {
                "rank": 2,
                "source_file": "redis_postmortem.pdf",
                "chunk_id": "redis_postmortem.pdf#retry",
                "heading_path": "事故复盘 > 根因",
                "content": (
                    "历史事故复盘表明 retry amplification 放大连接需求，"
                    "导致 connected_clients 逼近 maxclients。"
                ),
            },
        ],
    }

    frozen = build_frozen_generation_evidence(plan, payload)

    assert {binding.source_role for binding in frozen.bindings} >= {
        "official",
        "postmortem",
    }
    assert frozen.missing_subgoals == ()
    assert frozen.missing_entities == ()


def test_frozen_excerpt_is_selected_once_and_formatted_verbatim(monkeypatch) -> None:
    plan = build_question_plan("Redis 容量是否安全，并结合历史复盘说明")
    required_marker = "effective_capacity = maxclients - reserved_connections"
    payload = {
        "status": "success",
        "query": plan.query,
        "retrieval_results": [
            {
                "rank": 1,
                "source_file": "official_redis_clients.md",
                "chunk_id": "official_redis_clients.md#middle",
                "heading_path": "Redis 容量限制",
                "content": (
                    ("无关背景。" * 300)
                    + f"Redis 容量判断公式：{required_marker}；该值决定容量是否安全。"
                    + ("无关附录。" * 300)
                ),
            },
            {
                "rank": 2,
                "source_file": "redis_postmortem.pdf",
                "chunk_id": "redis_postmortem.pdf#history",
                "heading_path": "历史复盘",
                "content": "历史复盘显示 retry amplification 会放大 Redis 连接压力。",
            },
        ],
    }
    real_selector = rag_evidence_plan.select_generation_excerpt
    calls = 0

    def count_selector(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_selector(*args, **kwargs)

    monkeypatch.setattr(rag_evidence_plan, "select_generation_excerpt", count_selector)

    frozen = build_frozen_generation_evidence(plan, payload, limit=1800)
    calls_after_freeze = calls
    context = format_frozen_generation_context(frozen)

    assert calls_after_freeze == len(payload["retrieval_results"])
    assert calls == calls_after_freeze
    assert required_marker in context
    assert all(item["content"] in context for item in frozen.items)


def test_source_roles_never_label_static_knowledge_as_current() -> None:
    assert classify_source_role({"source_file": "redis_postmortem.pdf"}) == "postmortem"
    assert classify_source_role({"source_file": "tickets.xlsx"}) == "ticket"
    assert (
        classify_source_role(
            {
                "source_file": "incident_rows.json",
                "metadata": {"doc_type": "table"},
            }
        )
        == "ticket"
    )
    assert (
        classify_source_role(
            {
                "source_file": "redis_limits.md",
                "metadata": {"source_role": "official_snapshot"},
            }
        )
        == "official"
    )
    assert (
        classify_source_role(
            {
                "source_file": "redis_limits.md",
                "metadata": {"snapshot_type": "official"},
            }
        )
        == "official"
    )
    assert classify_source_role({"source_file": "redis_capacity_wiki.html"}) == "runbook"


def test_frozen_evidence_fails_closed_when_required_source_cannot_fit() -> None:
    plan = build_question_plan("Redis connected_clients 如何结合官方限制和事故复盘判断？")
    payload = {
        "status": "success",
        "query": plan.query,
        "required_sources": ["official_redis_clients.md", "redis_postmortem.pdf"],
        "retrieval_results": [
            {
                "source_file": "official_redis_clients.md",
                "chunk_id": "official_redis_clients.md#1",
                "content": "official capacity evidence",
            },
            {
                "source_file": "redis_postmortem.pdf",
                "chunk_id": "redis_postmortem.pdf#1",
                "content": "historical retry evidence",
            },
        ],
    }

    frozen = build_frozen_generation_evidence(plan, payload, limit=40)

    assert frozen.items == ()
    assert frozen.bindings == ()
