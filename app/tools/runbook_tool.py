"""Runbook retrieval tool backed by the existing RAG knowledge tool."""

from __future__ import annotations

from typing import Any

from app.core.resilience import run_bounded_sync_call
from app.services.rag_read_models import build_runbook_summary, compact_retrieval_payload
from app.services.rag_retrieval_service import retrieve_structured_knowledge
from app.tools.base import AIOpsTool, ToolRetryPolicy, clamp_int


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
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    timeout_seconds = 12.0
    retry_policy = ToolRetryPolicy(
        max_attempts=2,
        backoff_seconds=0.1,
        retry_on=["timeout", "connection_error", "server_error"],
    )
    data_sources = ["Milvus knowledge base", "lexical index"]
    degradation_strategy = "知识库不可用或可信度不足时拒绝强答，并返回 no_answer/failed 检索结果"

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        query = input_args.get("query") or input_args.get("symptom") or ""
        top_k = input_args.get("top_k")
        bounded_top_k = (
            clamp_int(top_k, default=5, minimum=1, maximum=10) if top_k is not None else None
        )
        if bounded_top_k is not None:
            input_args["top_k"] = bounded_top_k
        raw_payload = await run_bounded_sync_call(
            "rag-retrieval",
            "search_runbook",
            lambda: retrieve_structured_knowledge(
                str(query),
                top_k=bounded_top_k,
            ),
            timeout_seconds=self.timeout_seconds,
        )
        payload = compact_retrieval_payload(raw_payload)
        payload["summary"] = build_runbook_summary(payload)
        return payload
