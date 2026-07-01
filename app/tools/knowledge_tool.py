"""知识检索工具 - 从向量数据库中检索相关信息"""

from typing import Any

from langchain_core.documents import Document
from langchain_core.tools import tool
from loguru import logger

from app.services.rag_retrieval_service import documents_to_context, retrieve_structured_knowledge


@tool(response_format="content_and_artifact")
def retrieve_knowledge(query: str) -> tuple[str, dict[str, Any]]:
    """从知识库中检索相关信息来回答问题

    当用户的问题涉及专业知识、文档内容或需要参考资料时，使用此工具。

    Args:
        query: 用户的问题或查询

    Returns:
        Tuple[str, dict]: (格式化的上下文文本, 结构化检索结果)
    """
    logger.info(f"知识检索工具被调用: query='{query}'")
    payload = retrieve_structured_knowledge(query)
    logger.info(
        "知识检索完成: "
        f"status={payload.get('status')}, trusted={len(payload.get('retrieval_results', []))}, "
        f"rejected={len(payload.get('rejected_results', []))}"
    )
    return str(payload.get("content") or ""), payload


def format_docs(docs: list[Document]) -> str:
    """
    Legacy 兼容包装：格式化文档列表为上下文文本。

    新的 RAG 问答和 AIOps Runbook 主链路应优先使用
    retrieve_structured_knowledge 返回的结构化 payload。

    Args:
        docs: 文档列表

    Returns:
        str: 格式化的上下文文本
    """
    return documents_to_context(docs)
