"""Answer-policy helpers for grounded RAG responses."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from app.services.rag_read_models import format_score
from app.services.rag_retrieval_service import NO_TRUSTED_KNOWLEDGE


def build_grounded_question(question: str, retrieval_payload: dict[str, Any]) -> str:
    """Build the final LLM prompt from trusted retrieval context."""
    context = str(retrieval_payload.get("content") or "").strip()
    return (
        "请只基于下面的知识库检索结果回答用户问题。"
        "不要使用未出现在知识库中的事实；如果知识不足，请明确说明无法回答。\n\n"
        f"{context}\n\n"
        f"用户问题: {question}\n\n"
        "回答要求: 将答案和依据说清楚，末尾必须列出引用来源，格式为 "
        "source_file + chunk_id。"
    )


def build_grounded_system_prompt() -> str:
    """Return the strict system prompt used for tool-free grounded generation."""
    return (
        "你是知识库问答助手。你不能调用工具，也不能补充知识库之外的事实。"
        "如果检索片段不足以回答，请明确说明无法从当前知识库确认。"
    )


def message_content_to_text(content: Any) -> str:
    """Render LangChain message content into a string response."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def copy_message_with_content(message: BaseMessage, content: str) -> BaseMessage:
    """Return a copy of a LangChain message with replaced content."""
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"content": content})
    if hasattr(message, "copy"):
        return message.copy(update={"content": content})
    if isinstance(message, HumanMessage):
        return HumanMessage(content=content)
    return message


def build_no_answer_message(retrieval_payload: dict[str, Any]) -> str:
    """Return a stable refusal message when no trusted RAG source is available."""
    summary = str(retrieval_payload.get("summary") or NO_TRUSTED_KNOWLEDGE)
    if retrieval_payload.get("status") == "failed":
        return f"{summary}。知识库检索暂不可用，请检查向量库或检索配置后重试。"

    rejected = retrieval_payload.get("rejected_results") or []
    suffix = ""
    if rejected:
        suffix = "\n\n已检索到候选片段，但距离分数超过可信阈值，已拒绝强答。"
    return f"{summary}请补充相关知识库文档后再提问。{suffix}"


def build_missing_citation_message() -> str:
    """Return a stable refusal when trusted chunks cannot be cited."""
    return (
        "检索到候选知识，但缺少可审计引用信息，已拒绝生成回答。"
        "请重新索引文档或补齐 source_file 与 chunk_id 后再提问。"
    )


def build_citation_guard_payload(retrieval_payload: dict[str, Any]) -> dict[str, Any]:
    """Mark a successful retrieval as unusable when citations are incomplete."""
    guarded = dict(retrieval_payload)
    guarded["status"] = "no_answer"
    guarded["summary"] = "检索到候选知识，但缺少可审计引用信息。"
    guarded["answer_policy"] = "refuse_without_citation"
    guarded["no_answer_rejected"] = True
    guarded["rejected_results"] = list(guarded.get("retrieval_results") or [])
    guarded["retrieval_results"] = []
    return guarded


def has_valid_citations(citations: list[dict[str, Any]]) -> bool:
    """Require at least one source_file + chunk_id citation before answering."""
    for item in citations:
        source_file = str(item.get("source_file") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        if source_file and source_file != "未知来源" and chunk_id:
            return True
    return False


def ensure_citation_block(answer: str, citations: list[dict[str, Any]]) -> str:
    """Append missing source_file/chunk_id references so grounded answers stay auditable."""
    clean_answer = str(answer or "").strip()
    if not citations:
        return clean_answer

    missing = []
    for item in citations:
        source_file = str(item.get("source_file") or "未知来源")
        chunk_id = str(item.get("chunk_id") or "unknown")
        if source_file not in clean_answer or chunk_id not in clean_answer:
            missing.append((source_file, chunk_id, item.get("score"), item))

    if not missing:
        return clean_answer

    lines = ["", "", "引用来源："]
    for source_file, chunk_id, score, item in missing:
        score_text = format_score(score)
        locator_parts = [f"source_file: {source_file}", f"chunk_id: {chunk_id}"]
        if item.get("page_number"):
            locator_parts.append(f"page_number: {item.get('page_number')}")
        if item.get("sheet_name"):
            locator_parts.append(f"sheet_name: {item.get('sheet_name')}")
        if item.get("row_number"):
            locator_parts.append(f"row_number: {item.get('row_number')}")
        if item.get("primary_key"):
            locator_parts.append(f"primary_key: {item.get('primary_key')}")
        locator_parts.append(f"score: {score_text}")
        lines.append("- " + "; ".join(locator_parts))
    return clean_answer + "\n".join(lines)
