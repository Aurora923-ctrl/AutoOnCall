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
    build_generation_context,
    build_grounded_question,
    is_explicit_knowledge_refusal,
    select_supporting_citations,
)


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
        "结论来自 [redis.md#0002]。",
        citations,
    )

    assert selected == [{"source_file": "redis.md", "chunk_id": "redis.md#0002"}]


def test_select_supporting_citations_does_not_fallback_to_all_top_k() -> None:
    citations = [
        {"source_file": "redis.md", "chunk_id": "redis.md#0001"},
        {"source_file": "redis.md", "chunk_id": "redis.md#0002"},
    ]

    assert select_supporting_citations("只给出结论但没有 claim 引用。", citations) == []


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
    history = service.get_session_history("session-grounded")
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[0]["content"] == "Redis timeout 怎么处理？"
    assert "redis.md#0001" in history[1]["content"]


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
    assert not any(
        event.get("node") == "citation_guard"
        for event in events
        if event.get("type") == "content"
    )


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
