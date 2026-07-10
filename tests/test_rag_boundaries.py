"""RAG dependency boundary tests."""

import importlib
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

    service = embedding_module.DashScopeEmbeddings(api_key="test-key")
    service.client = SimpleNamespace(embeddings=FakeEmbeddingsClient())

    embeddings = service.embed_documents(["a", "bb", "ccc"])

    assert calls == [["a", "bb"], ["a", "bb"], ["ccc"]]
    assert embeddings == [[1.0, 1.0], [2.0, 1.0], [3.0, 1.0]]


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
