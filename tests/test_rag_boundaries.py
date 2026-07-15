"""RAG dependency boundary tests."""

import importlib
import math
from types import SimpleNamespace

import pytest

embedding_module = importlib.import_module("app.services.vector_embedding_service")
vector_store_module = importlib.import_module("app.services.vector_store_manager")
rag_agent_module = importlib.import_module("app.services.rag_agent_service")


def test_lazy_embedding_does_not_require_api_key_until_first_call(monkeypatch) -> None:
    monkeypatch.setattr(embedding_module.config, "dashscope_api_key", "")
    service = embedding_module.LazyDashScopeEmbeddings()

    assert service._service is None
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        service.embed_query("order-service timeout")


def test_dashscope_embedding_batches_documents_and_retries(monkeypatch) -> None:
    calls: list[list[str]] = []
    attempts = {"count": 0}

    class FakeEmbeddingsClient:
        def create(self, *, model, input, dimensions, encoding_format):
            attempts["count"] += 1
            batch = list(input)
            calls.append(batch)
            if attempts["count"] == 1:
                raise RuntimeError("temporary provider error")
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[float(len(text)), 1.0]) for text in batch]
            )

    monkeypatch.setattr(embedding_module.config, "dashscope_embedding_batch_size", 2)
    monkeypatch.setattr(embedding_module.config, "dashscope_embedding_max_retries", 1)
    monkeypatch.setattr(embedding_module.time, "sleep", lambda _seconds: None)

    service = embedding_module.DashScopeEmbeddings(api_key="test-key", dimensions=2)
    service.client = SimpleNamespace(embeddings=FakeEmbeddingsClient())

    embeddings = service.embed_documents(["a", "bb", "ccc"])

    assert calls == [["a", "bb"], ["a", "bb"], ["ccc"]]
    assert all(
        math.isclose(math.sqrt(sum(value * value for value in item)), 1.0) for item in embeddings
    )


def test_dashscope_embedding_configures_bounded_sdk_timeout_and_disables_sdk_retries(
    monkeypatch,
) -> None:
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(embedding_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(embedding_module.config, "dashscope_embedding_timeout_seconds", 12.5)

    embedding_module.DashScopeEmbeddings(api_key="test-key", dimensions=2)

    assert captured["timeout"] == 12.5
    assert captured["max_retries"] == 0


def test_dashscope_embedding_log_mask_never_exposes_key_characters() -> None:
    api_key = "sk-sensitive-secret-value"

    masked = embedding_module.DashScopeEmbeddings._mask_api_key(api_key)

    assert masked == "configured"
    assert not any(fragment in masked for fragment in ("sk-", "sensitive", "value"))


def test_dashscope_query_embedding_retries_with_same_bounded_policy(monkeypatch) -> None:
    attempts = {"count": 0}

    class FakeEmbeddingsClient:
        def create(self, **_kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("temporary provider error")
            return SimpleNamespace(data=[SimpleNamespace(embedding=[1.0, 2.0])])

    monkeypatch.setattr(embedding_module.config, "dashscope_embedding_max_retries", 1)
    monkeypatch.setattr(embedding_module.time, "sleep", lambda _seconds: None)
    service = embedding_module.DashScopeEmbeddings(api_key="test-key", dimensions=2)
    service.client = SimpleNamespace(embeddings=FakeEmbeddingsClient())

    embedding = service.embed_query("Redis timeout")
    assert math.isclose(math.sqrt(sum(value * value for value in embedding)), 1.0)
    assert attempts["count"] == 2


def test_dashscope_embedding_rejects_wrong_dimension_and_non_finite_values(monkeypatch) -> None:
    monkeypatch.setattr(embedding_module.config, "dashscope_embedding_max_retries", 0)
    service = embedding_module.DashScopeEmbeddings(api_key="test-key", dimensions=2)

    class FakeEmbeddingsClient:
        def __init__(self) -> None:
            self.responses = [
                SimpleNamespace(data=[SimpleNamespace(embedding=[1.0])]),
                SimpleNamespace(data=[SimpleNamespace(embedding=[1.0, float("nan")])]),
            ]

        def create(self, **_kwargs):
            return self.responses.pop(0)

    service.client = SimpleNamespace(embeddings=FakeEmbeddingsClient())

    with pytest.raises(RuntimeError, match="维度不一致"):
        service.embed_query("first")
    with pytest.raises(RuntimeError, match="NaN"):
        service.embed_query("second")


def test_dashscope_embedding_rejects_zero_vectors(monkeypatch) -> None:
    monkeypatch.setattr(embedding_module.config, "dashscope_embedding_max_retries", 0)
    service = embedding_module.DashScopeEmbeddings(api_key="test-key", dimensions=2)
    service.client = SimpleNamespace(
        embeddings=SimpleNamespace(
            create=lambda **_kwargs: SimpleNamespace(data=[SimpleNamespace(embedding=[0.0, 0.0])])
        )
    )

    with pytest.raises(RuntimeError, match="零向量"):
        service.embed_query("zero")


def test_vector_store_manager_does_not_connect_milvus_during_construction(monkeypatch) -> None:
    def fail_connect():
        raise RuntimeError("milvus unavailable")

    monkeypatch.setattr(vector_store_module.milvus_manager, "connect", fail_connect)

    manager = vector_store_module.VectorStoreManager()
    assert manager.vector_store is None

    with pytest.raises(RuntimeError, match="milvus unavailable"):
        manager.get_vector_store()


def test_vector_store_manager_skips_empty_document_list_without_connecting(monkeypatch) -> None:
    def fail_connect():
        raise RuntimeError("milvus unavailable")

    monkeypatch.setattr(vector_store_module.milvus_manager, "connect", fail_connect)

    manager = vector_store_module.VectorStoreManager()
    assert manager.add_documents([]) == []
    assert manager.vector_store is None


@pytest.mark.asyncio
async def test_vector_store_manager_closes_sync_and_async_clients() -> None:
    closed: list[str] = []

    class SyncClient:
        def close(self) -> None:
            closed.append("sync")

    class AsyncClient:
        async def close(self) -> None:
            closed.append("async")

    class FakeVectorStore:
        client = SyncClient()
        _async_milvus_client = AsyncClient()

    manager = vector_store_module.VectorStoreManager()
    manager.vector_store = FakeVectorStore()  # type: ignore[assignment]

    await manager.aclose()

    assert closed == ["async", "sync"]
    assert manager.vector_store is None


def test_rag_agent_does_not_create_model_during_construction(monkeypatch) -> None:
    monkeypatch.setattr(rag_agent_module.config, "dashscope_api_key", "")

    service = rag_agent_module.RagAgentService()

    assert service.model is None
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        service._ensure_model()


def test_rag_history_keeps_original_question_after_grounding() -> None:
    original_question = "Redis timeout 如何处理？"
    grounded_question = rag_agent_module.build_grounded_question(
        original_question,
        {
            "content": "Redis timeout runbook content",
        },
    )
    checkpoint = {
        "channel_values": {
            "messages": [rag_agent_module.HumanMessage(content=grounded_question)],
        },
    }

    class FakeCheckpointer:
        def get(self, _config):
            return checkpoint

    service = rag_agent_module.RagAgentService()
    service.checkpointer = FakeCheckpointer()

    service._replace_latest_human_message(
        session_id="session-rag-history",
        stored_question=grounded_question,
        display_question=original_question,
    )
    history = service.get_session_history("session-rag-history")

    assert history[0]["role"] == "user"
    assert history[0]["content"] == original_question
    assert "知识库检索结果" not in history[0]["content"]


@pytest.mark.asyncio
async def test_rag_agent_initialization_falls_back_when_mcp_is_unavailable(monkeypatch) -> None:
    created = {}

    async def fail_mcp_client():
        raise RuntimeError("mcp unavailable")

    def fake_create_agent(model, tools, checkpointer):
        created["model"] = model
        created["tools"] = tools
        created["checkpointer"] = checkpointer
        return object()

    monkeypatch.setattr(rag_agent_module.config, "dashscope_api_key", "test-key")
    monkeypatch.setattr(rag_agent_module, "ChatQwen", lambda **_kwargs: object())
    monkeypatch.setattr(rag_agent_module, "get_mcp_client_with_retry", fail_mcp_client)
    monkeypatch.setattr(rag_agent_module, "create_agent", fake_create_agent)

    service = rag_agent_module.RagAgentService()
    await service._initialize_agent()

    assert service._agent_initialized is True
    assert service.mcp_tools == []
    assert created["tools"] == service.tools
    assert created["checkpointer"] is service.checkpointer
