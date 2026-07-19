"""Unit tests for RAG answer citation helpers."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.services import rag_agent_service as rag_module
from app.services.rag_agent_service import (
    build_grounded_system_prompt,
    build_no_answer_message,
    compact_retrieval_payload,
    ensure_citation_block,
    has_valid_citations,
)
from app.services.rag_answer_policy import (
    answer_claims_are_cited,
    build_generation_context,
    build_generation_evidence,
    build_grounded_question,
    is_explicit_knowledge_refusal,
    remove_generic_uncertainty_boilerplate,
    select_supporting_citations,
    validated_citation_prefix,
)
from app.services.rag_read_models import compact_retrieval_chunk


def test_ensure_citation_block_appends_missing_sources() -> None:
    answer = "Redis timeout 通常需要先确认连接数。"
    citations = [
        {
            "source_file": "redis.md",
            "chunk_id": "redis.md#0001",
            "score": 0.12345,
        }
    ]

    grounded = ensure_citation_block(answer, citations)

    assert "引用来源" in grounded
    assert "source_file: redis.md" in grounded
    assert "chunk_id: redis.md#0001" in grounded
    assert "score: 0.1235" in grounded


def test_has_valid_citations_requires_source_file_and_chunk_id() -> None:
    assert has_valid_citations([{"source_file": "redis.md", "chunk_id": "redis.md#0001"}])
    assert not has_valid_citations([])
    assert not has_valid_citations([{"source_file": "redis.md", "chunk_id": ""}])
    assert not has_valid_citations([{"source_file": "未知来源", "chunk_id": "chunk-1"}])
    assert not has_valid_citations(
        [
            {"source_file": "redis.md", "chunk_id": "redis.md#0001"},
            {"source_file": "未知来源", "chunk_id": "chunk-2"},
        ]
    )


def test_explicit_knowledge_refusal_is_detected() -> None:
    assert is_explicit_knowledge_refusal("当前知识库无法回答该问题。")
    assert is_explicit_knowledge_refusal("知识库中没有关于差旅报销的信息。")
    assert not is_explicit_knowledge_refusal("请先检查 Redis connected_clients。")


def test_grounded_system_prompt_forbids_out_of_context_commands_and_tools() -> None:
    prompt = build_grounded_system_prompt()

    assert "命令" in prompt
    assert "工具名" in prompt
    assert "当前检索片段" in prompt
    assert "最多 4 条要点" in prompt
    assert "独立 claim" in prompt
    assert "部分答案" in prompt
    assert "通配符" in prompt
    assert "动作资格" in prompt
    assert "具体缺口" in prompt
    assert "每个必要来源" in prompt
    assert "不得遗漏" in prompt
    assert "原样引用" in prompt
    assert "完整回答不得追加泛化缺口" in prompt


def test_generation_context_deduplicates_same_content_without_mutating_retrieval() -> None:
    payload = {
        "retrieval_results": [
            {
                "source_file": "memory.md",
                "chunk_id": "memory.md#0002",
                "content": "检查 OOM 事件和内存指标。",
            },
            {
                "source_file": "memory.md",
                "chunk_id": "legacy-memory.md#0002",
                "content": "  检查 OOM 事件和内存指标。  ",
            },
            {
                "source_file": "memory.md",
                "chunk_id": "memory.md#0003",
                "content": "重启或扩容前必须审批并验证。",
            },
        ]
    }

    context = build_generation_context(payload)

    assert context.count("检查 OOM 事件和内存指标") == 1
    assert "chunk_id=memory.md#0002" in context
    assert "chunk_id=memory.md#0003" in context
    assert len(payload["retrieval_results"]) == 3


def test_generation_evidence_uses_basename_after_duplicate_content_is_collapsed() -> None:
    payload = {
        "retrieval_results": [
            {
                "source_file": "uploads/redis.md",
                "chunk_id": "redis.md#0001",
                "content": "Redis maxclients is constrained by the file descriptor limit.",
            },
            {
                "source_file": "docs/knowledge-base/redis.md",
                "chunk_id": "redis.md#0001",
                "content": "Redis maxclients is constrained by the file descriptor limit.",
            },
        ],
        "generation_allowlist": [
            {"source_file": "uploads/redis.md", "chunk_id": "redis.md#0001"},
            {
                "source_file": "docs/knowledge-base/redis.md",
                "chunk_id": "redis.md#0001",
            },
        ],
    }

    evidence = build_generation_evidence(payload)

    assert len(evidence) == 1
    assert evidence[0]["source_file"] == "redis.md"


def test_generation_evidence_matches_required_sources_by_basename() -> None:
    payload = {
        "retrieval_results": [
            {
                "source_file": "uploads/official_redis_clients.md",
                "chunk_id": "official_redis_clients.md#0004",
                "content": "Redis checks maxclients before accepting a connection.",
            },
            {
                "source_file": "docs/knowledge-base/redis_postmortem.pdf",
                "chunk_id": "redis_postmortem.pdf#0001",
                "content": "The incident window recorded connected client saturation.",
            },
        ],
        "required_sources": [
            "official_redis_clients.md",
            "redis_postmortem.pdf",
        ],
    }

    evidence = build_generation_evidence(payload)

    assert {item["source_file"] for item in evidence} == {
        "official_redis_clients.md",
        "redis_postmortem.pdf",
    }


def test_supporting_citations_accepts_labeled_grounded_reference() -> None:
    citations = [
        {
            "source_file": "payment_wiki.html",
            "chunk_id": "payment_wiki.html#0001",
        }
    ]

    selected = select_supporting_citations(
        "检查 active_connections。[source_file=payment_wiki.html; chunk_id=payment_wiki.html#0001]",
        citations,
    )

    assert selected == citations


def test_generation_context_deduplicates_near_duplicate_legacy_chunks() -> None:
    repeated = (
        "步骤1 获取当前时间。步骤2 查询系统监控日志。"
        "地域 ap-guangzhou，日志主题 system-metrics，时间范围最近30分钟。"
    )
    payload = {
        "retrieval_results": [
            {
                "source_file": "memory.md",
                "chunk_id": "memory.md#0003",
                "content": repeated + "查询条件 memory_usage:>85 OR event:OOM。",
            },
            {
                "source_file": "memory.md",
                "chunk_id": "legacy-memory.md#0002",
                "content": repeated,
            },
        ]
    }

    context = build_generation_context(payload)

    assert context.count("步骤1 获取当前时间") == 1
    assert "memory.md#0003" in context
    assert "legacy-memory.md#0002" not in context


def test_generation_context_keeps_richer_later_duplicate() -> None:
    shared = (
        "步骤1 获取当前时间。步骤2 查询系统监控日志。"
        "地域 ap-guangzhou，日志主题 system-metrics，时间范围最近30分钟。"
    )
    payload = {
        "retrieval_results": [
            {
                "source_file": "legacy-memory.md",
                "chunk_id": "legacy-memory.md#0002",
                "content": shared,
            },
            {
                "source_file": "memory.md",
                "chunk_id": "memory.md#0003",
                "content": shared + "重启前必须审批，并保留验证和回滚边界。",
            },
        ]
    }

    context = build_generation_context(payload)

    assert context.count("步骤1 获取当前时间") == 1
    assert "重启前必须审批" in context
    assert "chunk_id=memory.md#0003" in context
    assert "legacy-memory.md#0002" not in context


def test_generation_evidence_respects_complete_block_budget() -> None:
    payload = {
        "retrieval_results": [
            {
                "source_file": "one.md",
                "chunk_id": "one.md#0001",
                "content": "first evidence",
            },
            {
                "source_file": "two.md",
                "chunk_id": "two.md#0001",
                "content": "second evidence",
            },
        ]
    }

    first_block = build_generation_context({"retrieval_results": [payload["retrieval_results"][0]]})
    evidence = build_generation_evidence(payload, limit=len(first_block))

    assert [item["chunk_id"] for item in evidence] == ["one.md#0001"]
    assert len(build_generation_context({"retrieval_results": evidence})) <= len(first_block)


def test_generation_evidence_preserves_tail_of_first_oversized_block() -> None:
    payload = {
        "retrieval_results": [
            {
                "source_file": "runbook.md",
                "chunk_id": "runbook.md#0001",
                "content": ("background " * 100) + "ROLLBACK_REQUIRED",
            }
        ]
    }

    evidence = build_generation_evidence(payload, limit=180)

    assert len(evidence) == 1
    assert "ROLLBACK_REQUIRED" in evidence[0]["content"]
    assert "...<truncated>" in evidence[0]["content"]


def test_grounded_question_requires_claim_level_citation_and_concise_answer() -> None:
    prompt = build_grounded_question(
        "如何处理？",
        {
            "retrieval_results": [
                {
                    "source_file": "runbook.md",
                    "chunk_id": "runbook.md#0001",
                    "content": "先确认证据。",
                }
            ]
        },
    )

    assert "最多写 4 条要点" in prompt
    assert "直接提供" in prompt
    assert "[source_file | chunk_id]" in prompt
    assert "告警名只能作为检查线索" in prompt
    assert "不要使用“知识库无法回答”" in prompt
    assert "每个必要来源至少支持一条要点" in prompt
    assert "审批、dry-run、验证、回滚或人工接管边界" in prompt
    assert "不得替换成新的示例值" in prompt
    assert "只有确实存在未回答子问题时才写这句" in prompt
    assert "检查项 -> 如何判断 -> 证据边界" in prompt


def test_select_supporting_citations_keeps_only_chunks_named_by_answer() -> None:
    citations = [
        {"source_file": "redis.md", "chunk_id": "redis.md#0001"},
        {"source_file": "redis.md", "chunk_id": "redis.md#0002"},
        {"source_file": "redis.md", "chunk_id": "redis.md#0002"},
    ]

    selected = select_supporting_citations(
        "结论来自 [redis.md | redis.md#0002]。",
        citations,
    )

    assert selected == [{"source_file": "redis.md", "chunk_id": "redis.md#0002"}]


def test_generation_evidence_respects_explicit_allowlist() -> None:
    evidence = build_generation_evidence(
        {
            "retrieval_results": [
                {
                    "source_file": "redis.md",
                    "chunk_id": "redis.md#0001",
                    "content": "trusted",
                },
                {
                    "source_file": "noise.md",
                    "chunk_id": "noise.md#0001",
                    "content": "noise",
                },
            ],
            "generation_allowlist": [{"source_file": "redis.md", "chunk_id": "redis.md#0001"}],
        }
    )

    assert [(item["source_file"], item["chunk_id"]) for item in evidence] == [
        ("redis.md", "redis.md#0001")
    ]


def test_every_substantive_claim_requires_one_allowlisted_citation() -> None:
    allowed = {("redis.md", "redis.md#0001")}

    assert answer_claims_are_cited(
        "已知上下文事实：\n- 检查 maxclients。[redis.md | redis.md#0001]",
        allowed_pairs=allowed,
    )
    assert not answer_claims_are_cited(
        "- 检查 maxclients。[redis.md | redis.md#0001]\n- 当前连接已耗尽。",
        allowed_pairs=allowed,
    )


def test_one_claim_can_bind_multiple_allowlisted_citations() -> None:
    allowed = {
        ("payment_wiki.html", "payment_wiki.html#0001"),
        ("mysql_postmortem.pdf", "mysql_postmortem.pdf#0001"),
    }

    assert answer_claims_are_cited(
        "- 变更需要审批和窗口。"
        "[payment_wiki.html | payment_wiki.html#0001]"
        "[mysql_postmortem.pdf | mysql_postmortem.pdf#0001]",
        allowed_pairs=allowed,
    )


def test_validated_citation_prefix_only_releases_complete_allowlisted_claims() -> None:
    citations = [{"source_file": "redis.md", "chunk_id": "redis.md#0001"}]

    assert (
        validated_citation_prefix(
            "先检查连接数。[redis.md | redis.md#0001]后续草稿",
            citations,
        )
        == "先检查连接数。[redis.md | redis.md#0001]"
    )
    assert validated_citation_prefix("先检查连接数。", citations) == ""


def test_select_supporting_citations_does_not_fallback_to_all_top_k() -> None:
    citations = [
        {"source_file": "redis.md", "chunk_id": "redis.md#0001"},
        {"source_file": "redis.md", "chunk_id": "redis.md#0002"},
    ]

    assert select_supporting_citations("只给出结论但没有 claim 引用。", citations) == []


def test_generic_uncertainty_boilerplate_is_removed_before_citation_validation() -> None:
    answer = (
        "- 检查 active_connections。"
        "[payment_wiki.html | payment_wiki.html#0001]\n"
        "- 不确定项：当前片段未提供其余问题的依据。\n"
        "4. 当前片段未提供其余问题的依据：缺少具体命令。"
    )

    cleaned = remove_generic_uncertainty_boilerplate(answer)

    assert "不确定项" not in cleaned
    assert cleaned.endswith("[payment_wiki.html | payment_wiki.html#0001]")


def test_select_supporting_citations_does_not_accept_prefix_or_wrong_source() -> None:
    citations = [
        {"source_file": "redis.md", "chunk_id": "redis.md#0001"},
        {"source_file": "redis.md", "chunk_id": "redis.md#00010"},
    ]

    assert select_supporting_citations("[redis.md | redis.md#00010]", citations) == [citations[1]]
    assert select_supporting_citations("[redis.md#00010]", citations) == []
    assert select_supporting_citations("[other.md | redis.md#0001]", citations) == []
    assert (
        select_supporting_citations(
            "[other.md | redis.md#0001] [redis.md | redis.md#0001]",
            citations,
        )
        == []
    )


def test_ensure_citation_block_does_not_accept_wrong_source_with_valid_chunk_id() -> None:
    answer = "结论。[other.md | redis.md#0001]"
    citations = [
        {
            "source_file": "redis.md",
            "chunk_id": "redis.md#0001",
            "score": 0.12,
        }
    ]

    grounded = ensure_citation_block(answer, citations)

    assert "source_file: redis.md" in grounded
    assert "chunk_id: redis.md#0001" in grounded


def test_no_answer_payload_keeps_rejected_candidates_for_frontend() -> None:
    payload = {
        "status": "no_answer",
        "summary": "未找到可信知识来源。",
        "answer_policy": "refuse_without_trusted_source",
        "no_answer_rejected": True,
        "retrieval_results": [],
        "rejected_results": [
            {
                "source_file": "noise.md",
                "chunk_id": "noise.md#0001",
                "score": 8.0,
                "content_preview": "无关内容",
                "retrieval_reason": "L2 distance 8.0000 大于 阈值 0.5000",
            }
        ],
    }

    message = build_no_answer_message(payload)
    compact = compact_retrieval_payload(payload)

    assert "请补充相关知识库文档后再提问" in message
    assert compact["status"] == "no_answer"
    assert compact["no_answer_rejected"] is True
    assert compact["rejected_results"][0]["source_file"] == "noise.md"
    assert compact["rejected_results"][0]["source_path"] == "noise.md"
    assert compact["answer_policy"] == "refuse_without_trusted_source"


def test_compact_retrieval_payload_hides_absolute_source_path() -> None:
    payload = {
        "status": "success",
        "retrieval_degraded": True,
        "vector_error_message": "向量检索暂不可用，已降级使用本地词法索引。",
        "vector_error_type": "RuntimeError",
        "vector_error_detail": "http://milvus.internal:19530 unavailable",
        "retrieval_results": [
            {
                "source_file": "redis.md",
                "source_path": "/srv/autooncall/uploads/redis.md",
                "chunk_id": "redis.md#0001",
            }
        ],
    }

    compact = compact_retrieval_payload(payload)

    assert compact["retrieval_results"][0]["source_path"] == "redis.md"
    assert compact["retrieval_degraded"] is True
    assert compact["vector_error_message"] == "向量检索暂不可用，已降级使用本地词法索引。"
    assert compact["vector_error_type"] == "RuntimeError"
    assert "vector_error_detail" not in compact


def test_compact_retrieval_payload_keeps_thresholds_and_flat_location_fields() -> None:
    payload = {
        "min_lexical_trust_score": 0.2,
        "lexical_error_message": "词法检索暂不可用",
        "lexical_error_type": "RuntimeError",
        "retrieval_results": [
            {
                "source_file": "tickets.csv",
                "chunk_id": "tickets.csv#0001",
                "page_number": 0,
                "row_number": 0,
                "sheet_name": "Sheet1",
            }
        ],
    }

    compact = compact_retrieval_payload(payload)

    assert compact["min_lexical_trust_score"] == 0.2
    assert compact["lexical_error_type"] == "RuntimeError"
    assert compact["retrieval_results"][0]["page_number"] == 0
    assert compact["retrieval_results"][0]["row_number"] == 0
    assert compact["retrieval_results"][0]["sheet_name"] == "Sheet1"


def test_compact_retrieval_chunk_sanitizes_windows_paths_and_non_finite_scores() -> None:
    compact = compact_retrieval_chunk(
        {
            "doc_id": r"C:\srv\knowledge\redis.md",
            "source_file": r"C:\srv\knowledge\redis.md",
            "source_path": r"C:\srv\knowledge\redis.md",
            "chunk_id": "redis.md#0001",
            "score": float("nan"),
            "vector_score": float("inf"),
        }
    )

    assert compact["doc_id"] == "redis.md"
    assert compact["source_file"] == "redis.md"
    assert compact["source_path"] == "redis.md"
    assert compact["score"] is None
    assert compact["vector_score"] is None


def test_compact_retrieval_payload_normalizes_metadata_filter_and_boolean_score() -> None:
    compact = compact_retrieval_payload(
        {
            "metadata_filter": "service=billing",
            "retrieval_results": [
                {
                    "source_file": "redis.md",
                    "chunk_id": "redis.md#0001",
                    "score": True,
                }
            ],
        }
    )

    assert compact["metadata_filter"] == {}
    assert compact["retrieval_results"][0]["score"] == "True"


def test_citation_block_keeps_zero_based_document_locators() -> None:
    rendered = ensure_citation_block(
        "结论。",
        [
            {
                "source_file": "tickets.csv",
                "chunk_id": "tickets.csv#0001",
                "page_number": 0,
                "row_number": 0,
                "score": float("nan"),
            }
        ],
    )

    assert "page_number: 0" in rendered
    assert "row_number: 0" in rendered
    assert "score: unknown" in rendered


@pytest.mark.asyncio
async def test_query_with_retrieval_uses_tool_free_grounded_model(monkeypatch) -> None:
    class FakeGroundedModel:
        def __init__(self) -> None:
            self.messages = []

        async def ainvoke(self, messages):
            self.messages = messages
            return SimpleNamespace(
                content="根据知识库，先检查 Redis 连接数。[redis.md | redis.md#0001]"
            )

    service = rag_module.RagAgentService()
    service.model = FakeGroundedModel()

    async def fail_if_agent_initializes() -> None:
        raise AssertionError("grounded RAG answer must not initialize Agent tools")

    monkeypatch.setattr(service, "_initialize_agent", fail_if_agent_initializes)
    monkeypatch.setattr(
        rag_module,
        "retrieve_structured_knowledge",
        lambda *_args, **_kwargs: {
            "status": "success",
            "content": "source_file: redis.md\nchunk_id: redis.md#0001\nRedis 连接数过高会导致超时。",
            "summary": "检索到 1 条可信知识来源",
            "retrieval_results": [
                {
                    "source_file": "redis.md",
                    "chunk_id": "redis.md#0001",
                    "score": 0.12,
                    "content_preview": "Redis 连接数过高会导致超时。",
                }
            ],
            "rejected_results": [],
            "answer_policy": "answer_with_citations",
        },
    )

    result = await service.query_with_retrieval("Redis timeout 怎么处理？", "session-grounded")

    assert result["no_answer"] is False
    assert "redis.md#0001" in result["answer"]
    assert service.model.messages
    history = await service.get_session_history("session-grounded")
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[0]["content"] == "Redis timeout 怎么处理？"
    assert "redis.md#0001" in history[1]["content"]
    assert history[1]["metadata"]["citations"][0]["chunk_id"] == "redis.md#0001"
    assert history[1]["metadata"]["answerPolicy"] == "answer_with_citations"


@pytest.mark.asyncio
async def test_query_with_retrieval_only_allows_budgeted_generation_evidence(
    monkeypatch,
) -> None:
    class FakeGroundedModel:
        async def ainvoke(self, messages):
            prompt = str(messages[-1].content)
            assert "one.md#0001" in prompt
            assert "two.md#0001" not in prompt
            return SimpleNamespace(content="结论。[one.md | one.md#0001]")

    service = rag_module.RagAgentService()
    service.model = FakeGroundedModel()
    first = {
        "source_file": "one.md",
        "chunk_id": "one.md#0001",
        "content": "A" * 2900,
    }
    second = {
        "source_file": "two.md",
        "chunk_id": "two.md#0001",
        "content": "B" * 500,
    }
    monkeypatch.setattr(
        rag_module,
        "retrieve_structured_knowledge",
        lambda *_args, **_kwargs: {
            "status": "success",
            "retrieval_results": [first, second],
        },
    )

    result = await service.query_with_retrieval("question", "budgeted-evidence")

    assert [item["chunk_id"] for item in result["citations"]] == ["one.md#0001"]


@pytest.mark.asyncio
async def test_query_with_retrieval_keeps_frozen_evidence_allowlist_aligned(
    monkeypatch,
) -> None:
    class FakeGroundedModel:
        async def ainvoke(self, messages):
            prompt = str(messages[-1].content)
            assert "payment_wiki.html#0001" in prompt
            return SimpleNamespace(
                content="检查 active_connections 和 pool_waiting。[payment_wiki.html | payment_wiki.html#0001]"
            )

    service = rag_module.RagAgentService()
    service.model = FakeGroundedModel()
    monkeypatch.setattr(
        rag_module,
        "retrieve_structured_knowledge",
        lambda *_args, **_kwargs: {
            "status": "success",
            "retrieval_results": [
                {
                    "source_file": "uploads/payment_wiki.html",
                    "chunk_id": "payment_wiki.html#0001",
                    "content": "Check active_connections and pool_waiting.",
                }
            ],
            "generation_allowlist": [
                {
                    "source_file": "uploads/payment_wiki.html",
                    "chunk_id": "payment_wiki.html#0001",
                }
            ],
        },
    )

    result = await service.query_with_retrieval("How do I inspect MySQL?", "aligned-allowlist")

    assert result["no_answer"] is False
    assert result["citations"][0]["source_file"] == "payment_wiki.html"


@pytest.mark.asyncio
async def test_query_with_retrieval_refuses_generated_answer_without_claim_citation(
    monkeypatch,
) -> None:
    class FakeGroundedModel:
        async def ainvoke(self, _messages):
            return SimpleNamespace(content="根据知识库，先检查 Redis 连接数。")

    service = rag_module.RagAgentService()
    service.model = FakeGroundedModel()
    monkeypatch.setattr(
        rag_module,
        "retrieve_structured_knowledge",
        lambda *_args, **_kwargs: {
            "status": "success",
            "content": "Redis 连接数过高会导致超时。",
            "retrieval_results": [
                {
                    "source_file": "redis.md",
                    "chunk_id": "redis.md#0001",
                    "score": 0.12,
                    "content": "Redis 连接数过高会导致超时。",
                }
            ],
        },
    )

    result = await service.query_with_retrieval("Redis timeout 怎么处理？", "claim-citation")

    assert result["no_answer"] is True
    assert result["answer_policy"] == "refuse_without_citation"
    assert result["citations"] == []


@pytest.mark.asyncio
async def test_query_with_retrieval_refuses_success_payload_without_citations(monkeypatch) -> None:
    class NeverCalledModel:
        async def ainvoke(self, messages):  # pragma: no cover - defensive assertion
            raise AssertionError(
                "RAG must refuse before model generation when citations are missing"
            )

    service = rag_module.RagAgentService()
    service.model = NeverCalledModel()

    monkeypatch.setattr(
        rag_module,
        "retrieve_structured_knowledge",
        lambda *_args, **_kwargs: {
            "status": "success",
            "content": "source_file: \nchunk_id: \nRedis 连接数过高会导致超时。",
            "summary": "检索到 1 条可信知识来源",
            "retrieval_results": [
                {
                    "source_file": "",
                    "chunk_id": "",
                    "score": 0.12,
                    "content_preview": "Redis 连接数过高会导致超时。",
                }
            ],
            "rejected_results": [],
            "answer_policy": "answer_with_citations",
        },
    )

    result = await service.query_with_retrieval("Redis timeout 怎么处理？", "missing-citation")

    assert result["no_answer"] is True
    assert result["answer_policy"] == "refuse_without_citation"
    assert result["citations"] == []
    assert "缺少可审计引用信息" in result["answer"]
    assert result["retrieval"]["status"] == "no_answer"
    assert result["retrieval"]["no_answer_rejected"] is True


@pytest.mark.asyncio
async def test_query_with_retrieval_converts_model_knowledge_refusal(monkeypatch) -> None:
    class FakeGroundedModel:
        async def ainvoke(self, _messages):
            return SimpleNamespace(
                content="当前知识库无法回答该问题。",
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 6,
                    "total_tokens": 16,
                },
            )

    service = rag_module.RagAgentService(streaming=False)
    service.model = FakeGroundedModel()
    monkeypatch.setattr(
        rag_module,
        "retrieve_structured_knowledge",
        lambda *_args, **_kwargs: {
            "status": "success",
            "content": "source_file: noise.md\nchunk_id: noise.md#0001\n无关运维内容。",
            "summary": "检索到 1 条候选知识。",
            "retrieval_results": [
                {
                    "source_file": "noise.md",
                    "chunk_id": "noise.md#0001",
                    "score": 1.2,
                    "content_preview": "无关运维内容。",
                }
            ],
            "observability": {"stages": {"retrieval_total_ms": 12.0}},
        },
    )

    result = await service.query_with_retrieval("公司年假怎么申请？", "refusal-test")

    assert result["no_answer"] is True
    assert result["answer_policy"] == "refuse_without_trusted_source"
    assert result["citations"] == []
    assert result["observability"]["token_usage"]["status"] == "observed"


@pytest.mark.asyncio
async def test_query_stream_with_retrieval_converts_model_knowledge_refusal(monkeypatch) -> None:
    class FakeGroundedModel:
        async def astream(self, _messages):
            yield SimpleNamespace(content="当前知识库无法回答该问题。")

    service = rag_module.RagAgentService(streaming=True)
    service.model = FakeGroundedModel()
    monkeypatch.setattr(
        rag_module,
        "retrieve_structured_knowledge",
        lambda *_args, **_kwargs: {
            "status": "success",
            "content": "source_file: noise.md\nchunk_id: noise.md#0001\n无关运维内容。",
            "summary": "检索到 1 条候选知识。",
            "retrieval_results": [
                {
                    "source_file": "noise.md",
                    "chunk_id": "noise.md#0001",
                    "score": 1.2,
                    "content_preview": "无关运维内容。",
                }
            ],
        },
    )

    events = [
        event
        async for event in service.query_stream_with_retrieval(
            "公司年假怎么申请？",
            "stream-refusal-test",
        )
    ]

    complete = events[-1]["data"]
    assert complete["no_answer"] is True
    assert complete["answer_policy"] == "refuse_without_trusted_source"
    assert complete["citations"] == []
    assert complete["retrieval"]["status"] == "success"
    contents = [event for event in events if event.get("type") == "content"]
    assert len(contents) == 1
    assert contents[0]["node"] == "retrieval_guard"
    assert "当前知识库没有足够的相关证据" in contents[0]["data"]


@pytest.mark.asyncio
async def test_query_stream_with_retrieval_does_not_emit_unverified_model_content(
    monkeypatch,
) -> None:
    class FakeGroundedModel:
        async def astream(self, _messages):
            yield SimpleNamespace(content="未经引用门禁的草稿。")

    service = rag_module.RagAgentService(streaming=True)
    service.model = FakeGroundedModel()
    monkeypatch.setattr(
        rag_module,
        "retrieve_structured_knowledge",
        lambda *_args, **_kwargs: {
            "status": "success",
            "retrieval_results": [
                {
                    "source_file": "redis.md",
                    "chunk_id": "redis.md#0001",
                    "score": 0.12,
                    "content": "检查 Redis 连接数。",
                }
            ],
        },
    )

    events = [
        event
        async for event in service.query_stream_with_retrieval(
            "Redis timeout 怎么处理？",
            "stream-citation-guard",
        )
    ]

    content_events = [event for event in events if event.get("type") == "content"]
    assert len(content_events) == 1
    assert content_events[0]["node"] == "citation_guard"
    assert "未经引用门禁的草稿" not in content_events[0]["data"]
    assert events[-1]["data"]["answer_policy"] == "refuse_without_citation"


@pytest.mark.asyncio
async def test_query_stream_with_retrieval_emits_only_validated_final_answer(
    monkeypatch,
) -> None:
    class FakeGroundedModel:
        async def astream(self, _messages):
            yield SimpleNamespace(content="先检查 Redis 连接数。")
            yield SimpleNamespace(content="[redis.md | redis.md#0001]")

    service = rag_module.RagAgentService(streaming=True)
    service.model = FakeGroundedModel()
    monkeypatch.setattr(
        rag_module,
        "retrieve_structured_knowledge",
        lambda *_args, **_kwargs: {
            "status": "success",
            "retrieval_results": [
                {
                    "source_file": "redis.md",
                    "chunk_id": "redis.md#0001",
                    "score": 0.12,
                    "content": "检查 Redis 连接数。",
                }
            ],
        },
    )

    events = [
        event
        async for event in service.query_stream_with_retrieval(
            "Redis timeout 怎么处理？",
            "stream-valid-citation",
        )
    ]

    content_events = [event for event in events if event.get("type") == "content"]
    assert content_events == [
        {
            "type": "content",
            "data": "先检查 Redis 连接数。[redis.md | redis.md#0001]",
            "node": "citation_guard",
        }
    ]
    assert events[-1]["data"]["answer"] == content_events[0]["data"]
    assert events[-1]["data"]["no_answer"] is False


@pytest.mark.asyncio
async def test_query_stream_with_retrieval_releases_validated_claim_before_model_finishes(
    monkeypatch,
) -> None:
    release_second_chunk = asyncio.Event()

    class DelayedGroundedModel:
        async def astream(self, _messages):
            yield SimpleNamespace(content="第一条。[redis.md | redis.md#0001]")
            await release_second_chunk.wait()
            yield SimpleNamespace(content="第二条。[redis.md | redis.md#0001]")

    service = rag_module.RagAgentService(streaming=True)
    service.model = DelayedGroundedModel()
    monkeypatch.setattr(
        rag_module,
        "retrieve_structured_knowledge",
        lambda *_args, **_kwargs: {
            "status": "success",
            "retrieval_results": [
                {
                    "source_file": "redis.md",
                    "chunk_id": "redis.md#0001",
                    "content": "第一条和第二条。",
                }
            ],
        },
    )

    stream = service.query_stream_with_retrieval("question", "incremental-stream")
    assert (await anext(stream))["type"] == "search_results"
    first_content_task = asyncio.create_task(anext(stream))

    first_content = await asyncio.wait_for(first_content_task, timeout=0.1)
    assert first_content["type"] == "content"
    assert first_content["data"] == "第一条。[redis.md | redis.md#0001]"
    assert not release_second_chunk.is_set()

    release_second_chunk.set()
    remaining = [event async for event in stream]
    assert remaining[0]["data"] == "第二条。[redis.md | redis.md#0001]"
    assert remaining[-1]["type"] == "complete"


@pytest.mark.asyncio
async def test_query_with_retrieval_offloads_sync_retrieval(monkeypatch) -> None:
    service = rag_module.RagAgentService()

    def slow_retrieve(*_args, **_kwargs):
        time.sleep(0.25)
        return {
            "status": "no_answer",
            "summary": "未找到可信知识来源。",
            "retrieval_results": [],
            "rejected_results": [],
            "answer_policy": "refuse_without_trusted_source",
        }

    monkeypatch.setattr(rag_module, "retrieve_structured_knowledge", slow_retrieve)

    started_at = time.perf_counter()
    task = asyncio.create_task(service.query_with_retrieval("Redis timeout", "session-offload"))
    await asyncio.sleep(0)
    elapsed_to_yield = time.perf_counter() - started_at
    result = await task

    assert elapsed_to_yield < 0.12
    assert result["no_answer"] is True


@pytest.mark.asyncio
async def test_same_session_turns_are_serialized_but_other_sessions_can_run(
    monkeypatch,
) -> None:
    service = rag_module.RagAgentService()
    active_by_session: dict[str, int] = {}
    max_active_by_session: dict[str, int] = {}
    globally_active = 0
    max_globally_active = 0

    async def fake_locked_query(question, session_id, metadata_filter=None):
        nonlocal globally_active, max_globally_active
        active_by_session[session_id] = active_by_session.get(session_id, 0) + 1
        max_active_by_session[session_id] = max(
            max_active_by_session.get(session_id, 0),
            active_by_session[session_id],
        )
        globally_active += 1
        max_globally_active = max(max_globally_active, globally_active)
        await asyncio.sleep(0.02)
        globally_active -= 1
        active_by_session[session_id] -= 1
        return {"answer": question}

    monkeypatch.setattr(service, "_query_with_retrieval_locked", fake_locked_query)

    results = await asyncio.gather(
        service.query_with_retrieval("first", "shared"),
        service.query_with_retrieval("second", "shared"),
        service.query_with_retrieval("other", "isolated"),
    )

    assert [item["answer"] for item in results] == ["first", "second", "other"]
    assert max_active_by_session["shared"] == 1
    assert max_globally_active >= 2
    assert service._session_locks == {}
    assert service._session_lock_users == {}


@pytest.mark.asyncio
async def test_clear_session_waits_for_inflight_turn(monkeypatch) -> None:
    service = rag_module.RagAgentService()
    turn_started = asyncio.Event()
    release_turn = asyncio.Event()
    calls: list[str] = []

    async def fake_locked_query(question, session_id, metadata_filter=None):
        calls.append("turn-start")
        turn_started.set()
        await release_turn.wait()
        calls.append("turn-end")
        service._append_grounded_history(session_id, question, "answer")
        return {"answer": "answer"}

    async def fake_delete_thread(session_id: str) -> None:
        calls.append(f"clear:{session_id}")

    monkeypatch.setattr(service, "_query_with_retrieval_locked", fake_locked_query)
    monkeypatch.setattr(service.checkpointer, "adelete_thread", fake_delete_thread)

    turn_task = asyncio.create_task(service.query_with_retrieval("question", "shared"))
    await turn_started.wait()
    clear_task = asyncio.create_task(service.clear_session("shared"))
    await asyncio.sleep(0)

    assert not clear_task.done()
    release_turn.set()
    await turn_task

    assert await clear_task is True
    assert calls == ["turn-start", "turn-end", "clear:shared"]
    assert await service.get_session_history("shared") == []
    assert service._session_locks == {}
    assert service._session_lock_users == {}


@pytest.mark.asyncio
async def test_get_session_history_propagates_checkpoint_failure(monkeypatch) -> None:
    service = rag_module.RagAgentService()

    async def fail_checkpoint(_session_id: str) -> dict:
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(service, "_aget_checkpoint_data", fail_checkpoint)

    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        await service.get_session_history("broken-history")

    assert service._session_locks == {}
    assert service._session_lock_users == {}


@pytest.mark.asyncio
async def test_grounded_model_retries_transient_failure(monkeypatch) -> None:
    attempts = 0

    class RetryModel:
        async def ainvoke(self, _messages):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("temporary timeout")
            return SimpleNamespace(content="answer")

    service = rag_module.RagAgentService()
    service.model = RetryModel()
    monkeypatch.setattr(rag_module.config, "rag_model_max_retries", 1)
    monkeypatch.setattr(rag_module.config, "rag_model_retry_delay_seconds", 0.0)

    answer = await service.query_grounded("prompt", "retry-session")

    assert answer == "answer"
    assert attempts == 2


@pytest.mark.asyncio
async def test_grounded_model_timeout_is_bounded(monkeypatch) -> None:
    cancelled = asyncio.Event()

    class HangingModel:
        async def ainvoke(self, _messages):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    service = rag_module.RagAgentService()
    service.model = HangingModel()
    monkeypatch.setattr(rag_module.config, "rag_model_timeout_seconds", 0.01)
    monkeypatch.setattr(rag_module.config, "rag_model_max_retries", 0)

    with pytest.raises(TimeoutError):
        await service.query_grounded("prompt", "timeout-session")

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_grounded_model_cancellation_is_not_retried(monkeypatch) -> None:
    attempts = 0
    started = asyncio.Event()

    class HangingModel:
        async def ainvoke(self, _messages):
            nonlocal attempts
            attempts += 1
            started.set()
            await asyncio.Event().wait()

    service = rag_module.RagAgentService()
    service.model = HangingModel()
    monkeypatch.setattr(rag_module.config, "rag_model_max_retries", 3)

    task = asyncio.create_task(service.query_grounded("prompt", "cancel-session"))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert attempts == 1


@pytest.mark.asyncio
async def test_grounded_stream_does_not_retry_after_provider_content(monkeypatch) -> None:
    attempts = 0

    class RetryStreamModel:
        async def astream(self, _messages):
            nonlocal attempts
            attempts += 1
            yield SimpleNamespace(content="partial")
            raise ConnectionError("temporary connection failure")

    service = rag_module.RagAgentService(streaming=True)
    service.model = RetryStreamModel()
    monkeypatch.setattr(rag_module.config, "rag_model_max_retries", 1)
    monkeypatch.setattr(rag_module.config, "rag_model_retry_delay_seconds", 0.0)

    events = []
    with pytest.raises(ConnectionError, match="temporary connection failure"):
        async for event in service.query_grounded_stream(
            "prompt",
            "stream-retry-session",
        ):
            events.append(event)

    assert [event.get("data") for event in events] == ["partial"]
    assert attempts == 1


@pytest.mark.asyncio
async def test_grounded_stream_rejects_output_over_configured_limit(monkeypatch) -> None:
    class LargeStreamModel:
        async def astream(self, _messages):
            yield SimpleNamespace(content="a" * 64)
            yield SimpleNamespace(content="b" * 64)

    service = rag_module.RagAgentService(streaming=True)
    service.model = LargeStreamModel()
    monkeypatch.setattr(rag_module.config, "rag_stream_spool_max_memory_bytes", 32)

    events = []
    with pytest.raises(ValueError, match="安全上限"):
        async for event in service.query_grounded_stream(
            "prompt",
            "stream-spool-session",
        ):
            events.append(event)

    assert events == []
