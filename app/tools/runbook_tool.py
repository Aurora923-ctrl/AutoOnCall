"""Runbook retrieval tool backed by the existing RAG knowledge tool."""

from __future__ import annotations

from typing import Any

from app.services.rag_read_models import build_runbook_summary
from app.services.rag_retrieval_service import retrieve_structured_knowledge
from app.tools.base import AIOpsTool


class SearchRunbookTool(AIOpsTool):
    """Search internal runbooks through the existing retrieve_knowledge tool."""

    name = "search_runbook"
    description = "检索内部 Runbook、故障处理手册和 AIOps 知识库"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer"},
        },
        "required": ["query"],
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    timeout_seconds = 12.0
    data_sources = ["Milvus knowledge base", "lexical index"]
    degradation_strategy = "知识库不可用或可信度不足时拒绝强答，并返回 no_answer/failed 检索结果"

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        query = input_args.get("query") or input_args.get("symptom") or ""
        top_k = input_args.get("top_k")
        payload = retrieve_structured_knowledge(
            str(query),
            top_k=int(top_k) if isinstance(top_k, int) and top_k > 0 else None,
        )
        payload["summary"] = build_runbook_summary(payload)
        return payload
