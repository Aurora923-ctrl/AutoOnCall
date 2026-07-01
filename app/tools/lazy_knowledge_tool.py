"""Lazy LangChain wrapper for the knowledge retrieval tool.

The real knowledge tool imports the RAG retrieval stack, which may initialize
Milvus-related objects when used. Keeping this wrapper as the public export lets
agents register the tool without forcing vector-store setup during app import.
"""

from typing import Any

from langchain_core.tools import tool


@tool(response_format="content_and_artifact")
def retrieve_knowledge(query: str) -> tuple[str, Any]:
    """从知识库中检索相关信息来回答问题，首次调用时才加载真实检索实现。"""
    from app.tools.knowledge_tool import retrieve_knowledge as real_retrieve_knowledge

    result = real_retrieve_knowledge.invoke({"query": query})
    if isinstance(result, tuple) and len(result) == 2:
        return result
    return str(result), []
