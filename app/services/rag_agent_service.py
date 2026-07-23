"""RAG Agent 服务 - 基于 LangGraph 的智能代理

使用 langchain_qwq 的 ChatQwen 原生集成，
支持真正的流式输出和更好的模型适配。
"""

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, cast

from langchain.agents import create_agent
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langchain_qwq import ChatQwen
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from loguru import logger
from typing_extensions import TypedDict

from app.agent.mcp_client import discover_safe_mcp_tools, get_mcp_client_with_retry
from app.config import config
from app.core.observability import dependency_operation
from app.core.resilience import CircuitOpenError, call_with_resilience, get_circuit_breaker
from app.services.rag_answer_contract import (
    AnswerContract,
    ContractViolation,
    validate_answer_contract,
)
from app.services.rag_answer_policy import (
    build_extractive_grounded_answer,
    build_generation_context,
    build_grounded_question,
    build_grounded_system_prompt,
    build_no_answer_message,
    copy_message_with_content,
    ensure_citation_block as _ensure_citation_block,
    has_valid_citations as _has_valid_citations,
    message_content_to_text,
    validated_citation_prefix,
)
from app.services.rag_generation_guard import (
    GroundedAnswerDecision,
    finalize_grounded_answer,
    prepare_grounded_generation,
)
from app.services.rag_read_models import compact_retrieval_payload
from app.services.rag_retrieval_service import (
    retrieve_structured_knowledge,
)
from app.tools import get_current_time, retrieve_knowledge
from app.utils.log_safety import sanitize_log_value, summarize_text_for_log

# Historical helper exports retained for callers and tests using this module as a facade.
ensure_citation_block = _ensure_citation_block
has_valid_citations = _has_valid_citations

# 阿里千问大模型和langchain集成参考： https://docs.langchain.com/oss/python/integrations/chat/qwen
# 注意：需要配置环境变量 DASHSCOPE_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1 否则默认访问的是新加坡站点
# 同时也需要配置环境变量 DASHSCOPE_API_KEY=your_api_key


class AgentState(TypedDict):
    """Agent 状态"""

    messages: Annotated[Sequence[BaseMessage], add_messages]


def trim_messages_middleware(state: AgentState) -> dict[str, Any] | None:
    """
    修剪消息历史，只保留最近的几条消息以适应上下文窗口

    策略：
    - 保留第一条系统消息（System Message）
    - 保留最近的 6 条消息（3 轮对话）
    - 当消息少于等于 7 条时，不做修剪

    Args:
        state: Agent 状态

    Returns:
        包含修剪后消息的字典，如果无需修剪则返回 None
    """
    messages = state["messages"]

    if len(messages) <= 7:
        return None

    first_msg = messages[0]

    recent_messages = messages[-6:] if len(messages) % 2 == 0 else messages[-7:]

    new_messages = [first_msg] + list(recent_messages)

    logger.debug(f"修剪消息历史: {len(messages)} -> {len(new_messages)} 条")

    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]}


class RagAgentService:
    """RAG Agent 服务 - 使用 LangGraph + ChatQwen 原生集成"""

    def __init__(self, streaming: bool = True):
        """初始化 RAG Agent 服务

        Args:
            streaming: 是否启用流式输出，默认为 True
        """
        self.model_name = config.effective_rag_model

        self.streaming = streaming

        self.system_prompt = self._build_system_prompt()

        self.model: Any | None = None

        self.tools = [retrieve_knowledge, get_current_time]

        self.mcp_tools: list = []

        self.checkpointer = MemorySaver()
        self._grounded_history: dict[str, list[dict[str, Any]]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_lock_users: dict[str, int] = {}

        self.agent: Any | None = None
        self._agent_initialized = False
        self._agent_initialization_lock = asyncio.Lock()

        logger.info(f"RAG Agent 服务初始化完成, model={self.model_name}, streaming={streaming}")

    async def _initialize_agent(self):
        """异步初始化 Agent（包括 MCP 工具）"""
        if self._agent_initialized:
            return

        async with self._agent_initialization_lock:
            if self._agent_initialized:
                return

            model = self._ensure_model()
            try:
                mcp_client = await get_mcp_client_with_retry()
                mcp_tools = await discover_safe_mcp_tools(mcp_client)
                logger.info(f"成功加载 {len(mcp_tools)} 个 MCP 工具")
            except Exception as exc:
                logger.warning(
                    "MCP 工具加载失败，RAG Agent 将仅使用本地工具: error_type={}",
                    type(exc).__name__,
                )
                mcp_tools = []

            self.mcp_tools = mcp_tools
            all_tools = self.tools + self.mcp_tools
            self.agent = create_agent(
                model,
                tools=all_tools,
                checkpointer=self.checkpointer,
            )
            self._agent_initialized = True

            if all_tools:
                tool_names = [
                    tool.name if hasattr(tool, "name") else str(tool) for tool in all_tools
                ]
                logger.info(f"可用工具列表: {', '.join(tool_names)}")

    def _build_system_prompt(self) -> str:
        """
        构建系统提示词

        注意：LangChain 框架会自动将工具信息传递给 LLM，
        因此系统提示词中无需列举具体的工具列表。

        Returns:
            str: 系统提示词
        """
        from textwrap import dedent

        return dedent("""
            你是一个专业的AI助手，能够使用多种工具来帮助用户解决问题。

            工作原则:
            1. 理解用户需求，选择合适的工具来完成任务
            2. 当需要获取实时信息或专业知识时，主动使用相关工具
            3. 基于工具返回的结果提供准确、专业的回答
            4. 如果工具无法提供足够信息，请诚实地告知用户

            回答要求:
            - 保持友好、专业的语气
            - 回答简洁明了，重点突出
            - 基于事实，不编造信息
            - 如有不确定的地方，明确说明
            - 如果使用知识库检索结果，回答末尾必须列出引用来源，格式为 source_file + chunk_id
            - 如果知识库工具返回"未找到可信知识来源"，请明确拒答并说明需要补充可信文档，不要凭空回答

            请根据用户的问题，灵活使用可用工具，提供高质量的帮助。
        """).strip()

    async def query(
        self,
        question: str,
        session_id: str,
        *,
        history_question: str | None = None,
    ) -> str:
        """
        非流式处理用户问题（一次性返回完整答案）

        Args:
            question: 用户问题
            session_id: 会话ID（作为 thread_id）

        Returns:
            str: 完整答案
        """
        safe_session_id = sanitize_log_value(session_id)
        try:
            await self._initialize_agent()
            agent = self._require_agent()

            logger.info(
                f"[会话 {safe_session_id}] RAG Agent 收到查询（非流式）: "
                f"{summarize_text_for_log(question, label='question')}"
            )

            messages = [SystemMessage(content=self.system_prompt), HumanMessage(content=question)]

            agent_input = {"messages": messages}

            config_dict = {"configurable": {"thread_id": session_id}}

            result = await agent.ainvoke(
                input=agent_input,
                config=config_dict,
            )
            self._replace_latest_human_message(
                session_id=session_id,
                stored_question=question,
                display_question=history_question,
            )

            messages_result = result.get("messages", [])
            if messages_result:
                last_message = messages_result[-1]
                answer = message_content_to_text(
                    last_message.content if hasattr(last_message, "content") else last_message
                )

                if hasattr(last_message, "tool_calls") and last_message.tool_calls:
                    tool_names = [tc.get("name", "unknown") for tc in last_message.tool_calls]
                    logger.info(f"[会话 {safe_session_id}] Agent 调用了工具: {tool_names}")

                logger.info(f"[会话 {safe_session_id}] RAG Agent 查询完成（非流式）")
                return answer

            logger.warning(f"[会话 {safe_session_id}] Agent 返回结果为空")
            return ""

        except Exception as exc:
            logger.error(
                "[会话 {}] RAG Agent 查询失败（非流式）: error_type={}",
                safe_session_id,
                type(exc).__name__,
            )
            raise

    async def query_with_retrieval(
        self,
        question: str,
        session_id: str,
        metadata_filter: dict[str, Any] | None = None,
        *,
        include_evaluation_context: bool = False,
    ) -> dict[str, Any]:
        """Answer a knowledge-base question with explicit retrieval citations."""
        async with self._session_scope(session_id):
            kwargs: dict[str, Any] = {"metadata_filter": metadata_filter}
            if include_evaluation_context:
                kwargs["include_evaluation_context"] = True
            return await self._query_with_retrieval_locked(question, session_id, **kwargs)

    async def _query_with_retrieval_locked(
        self,
        question: str,
        session_id: str,
        metadata_filter: dict[str, Any] | None = None,
        *,
        include_evaluation_context: bool = False,
    ) -> dict[str, Any]:
        """Run one non-streaming RAG turn while the session lock is held."""
        retrieval_payload = await asyncio.to_thread(
            retrieve_structured_knowledge,
            question,
            metadata_filter=metadata_filter,
        )
        if not str(retrieval_payload.get("query") or "").strip():
            retrieval_payload = {**retrieval_payload, "query": question}
        retrieval_context = compact_retrieval_payload(retrieval_payload)

        if retrieval_payload.get("status") != "success":
            answer = build_no_answer_message(retrieval_payload)
            self._append_grounded_history(
                session_id,
                question,
                answer,
                assistant_metadata=_rag_history_metadata(
                    citations=[],
                    retrieval=retrieval_context,
                    no_answer=True,
                    answer_policy=retrieval_payload.get(
                        "answer_policy", "refuse_without_trusted_source"
                    ),
                ),
            )
            return {
                "success": True,
                "answer": answer,
                "citations": [],
                "retrieval": retrieval_context,
                "no_answer": True,
                "answer_policy": retrieval_payload.get(
                    "answer_policy", "refuse_without_trusted_source"
                ),
            }

        preparation = prepare_grounded_generation(retrieval_payload)
        if preparation.refused:
            answer = preparation.refusal_answer
            guarded_context = preparation.refusal_context or retrieval_context
            self._append_grounded_history(
                session_id,
                question,
                answer,
                assistant_metadata=_rag_history_metadata(
                    citations=[],
                    retrieval=guarded_context,
                    no_answer=True,
                    answer_policy=preparation.refusal_policy,
                ),
            )
            return {
                "success": True,
                "answer": answer,
                "citations": [],
                "retrieval": guarded_context,
                "no_answer": True,
                "answer_policy": preparation.refusal_policy,
            }
        generation_payload = cast(dict[str, Any], preparation.generation_payload)
        citations = preparation.citations
        grounded_question = build_grounded_question(question, generation_payload)
        generation_started = time.perf_counter()
        answer, generation_observability = await self.query_grounded_observed(
            grounded_question,
            session_id,
            history_question=question,
        )
        decision, generation_observability = await self._finalize_with_contract_repair(
            original_answer=answer,
            citations=citations,
            retrieval_payload=retrieval_payload,
            retrieval_context=retrieval_context,
            generation_payload=generation_payload,
            answer_contract=preparation.answer_contract,
            question=question,
            session_id=session_id,
            generation_observability=generation_observability,
        )
        answer = decision.answer
        citations = decision.citations
        observability = build_rag_observability(
            retrieval_payload,
            generation_observability,
            total_ms=(
                float(
                    retrieval_payload.get("observability", {})
                    .get("stages", {})
                    .get("retrieval_total_ms", 0.0)
                    or 0.0
                )
                + round((time.perf_counter() - generation_started) * 1000, 2)
            ),
        )
        self._append_grounded_history(
            session_id,
            question,
            answer,
            assistant_metadata=_rag_history_metadata(
                citations=citations,
                retrieval=decision.retrieval_context,
                no_answer=decision.no_answer,
                answer_policy=decision.answer_policy,
            ),
        )

        response = {
            "success": True,
            "answer": answer,
            "citations": citations,
            "retrieval": decision.retrieval_context,
            "no_answer": decision.no_answer,
            "answer_policy": decision.answer_policy,
            "observability": observability,
        }
        if include_evaluation_context:
            response["_evaluation_contexts"] = [
                {
                    "source_file": str(item.get("source_file") or ""),
                    "chunk_id": str(item.get("chunk_id") or ""),
                    "content": str(
                        item.get("content") or item.get("content_preview") or ""
                    ).strip(),
                }
                for item in generation_payload["retrieval_results"]
            ]
        return response

    async def _finalize_with_contract_repair(
        self,
        *,
        original_answer: str,
        citations: list[dict[str, Any]],
        retrieval_payload: dict[str, Any],
        retrieval_context: dict[str, Any],
        generation_payload: dict[str, Any],
        answer_contract: AnswerContract | None,
        question: str,
        session_id: str,
        generation_observability: dict[str, Any],
    ) -> tuple[GroundedAnswerDecision, dict[str, Any]]:
        """Validate one answer, make one semantic repair, then fall back extractively."""
        evidence = generation_payload.get("retrieval_results", [])
        decision = finalize_grounded_answer(
            original_answer,
            citations,
            retrieval_payload,
            retrieval_context,
            evidence=evidence,
        )
        if decision.no_answer and decision.answer_policy == "refuse_without_trusted_source":
            return decision, generation_observability
        if answer_contract is None:
            return decision, generation_observability

        violations = validate_answer_contract(
            decision.answer,
            answer_contract,
            decision.citations,
        )
        if not violations and not decision.no_answer:
            return decision, generation_observability

        repair_prompt = _build_contract_repair_prompt(
            generation_payload,
            original_answer,
            violations,
            answer_contract,
        )
        repaired_answer, repair_observability = await self.query_grounded_observed(
            repair_prompt,
            session_id,
            history_question=question,
        )
        generation_observability = _merge_generation_observability(
            generation_observability,
            repair_observability,
        )
        generation_observability["repair_reason"] = "answer_contract"
        generation_observability["repair_sources"] = []
        repaired_decision = finalize_grounded_answer(
            repaired_answer,
            citations,
            retrieval_payload,
            retrieval_context,
            evidence=evidence,
        )
        repaired_violations = validate_answer_contract(
            repaired_decision.answer,
            answer_contract,
            repaired_decision.citations,
        )
        if not repaired_decision.no_answer and not repaired_violations:
            return repaired_decision, generation_observability

        extractive_answer = build_extractive_grounded_answer(
            question,
            evidence,
            required_sources=retrieval_payload.get("required_sources"),
            max_claims=answer_contract.max_claims,
            answer_contract=answer_contract,
        )
        extractive_decision = finalize_grounded_answer(
            extractive_answer,
            citations,
            retrieval_payload,
            retrieval_context,
            evidence=evidence,
        )
        extractive_violations = validate_answer_contract(
            extractive_decision.answer,
            answer_contract,
            extractive_decision.citations,
        )
        if not extractive_decision.no_answer and not extractive_violations:
            generation_observability["extractive_fallback_used"] = True
            return extractive_decision, generation_observability

        refusal = finalize_grounded_answer(
            "",
            citations,
            retrieval_payload,
            retrieval_context,
            evidence=evidence,
        )
        return refusal, generation_observability

    async def query_grounded(
        self,
        grounded_question: str,
        session_id: str,
        *,
        history_question: str | None = None,
    ) -> str:
        """Generate a grounded answer without exposing any Agent tools to the model."""
        model = self._ensure_model()
        safe_session_id = sanitize_log_value(session_id)
        logger.info(f"[会话 {safe_session_id}] RAG grounded 生成（禁用工具）")
        messages = [
            SystemMessage(content=build_grounded_system_prompt()),
            HumanMessage(content=grounded_question),
        ]
        result = await self._invoke_model_with_retry(model, messages, session_id=session_id)
        content = result.content if hasattr(result, "content") else result
        answer = message_content_to_text(content)
        if history_question:
            logger.debug(
                f"[会话 {safe_session_id}] grounded 问答原始问题: "
                f"{summarize_text_for_log(history_question, label='question')}"
            )
        return answer

    async def query_grounded_observed(
        self,
        grounded_question: str,
        session_id: str,
        *,
        history_question: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Generate one grounded answer and retain provider-reported usage when available."""
        model = self._ensure_model()
        started_at = time.perf_counter()
        messages = [
            SystemMessage(content=build_grounded_system_prompt()),
            HumanMessage(content=grounded_question),
        ]
        result = await self._invoke_model_with_retry(model, messages, session_id=session_id)
        content = result.content if hasattr(result, "content") else result
        answer = message_content_to_text(content)
        usage = extract_message_token_usage(result)
        return answer, {
            "llm_generation_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "llm_ttft_ms": "not_observed",
            "token_usage": usage
            or {
                "status": "not_observed",
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
            },
            "model": self.model_name,
        }

    async def query_stream(
        self,
        question: str,
        session_id: str,
        *,
        history_question: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        流式处理用户问题（逐步返回答案片段）

        Args:
            question: 用户问题
            session_id: 会话ID（作为 thread_id）

        Yields:
            Dict[str, Any]: 包含流式数据的字典
                - type: "content" | "tool_call" | "complete" | "error"
                - data: 具体内容
        """
        safe_session_id = sanitize_log_value(session_id)
        try:
            await self._initialize_agent()
            agent = self._require_agent()

            logger.info(
                f"[会话 {safe_session_id}] RAG Agent 收到查询（流式）: "
                f"{summarize_text_for_log(question, label='question')}"
            )

            # 构建消息列表（系统提示 + 用户问题）
            messages = [SystemMessage(content=self.system_prompt), HumanMessage(content=question)]

            # 构建 Agent 输入
            agent_input = {"messages": messages}

            config_dict = {"configurable": {"thread_id": session_id}}

            async for token, metadata in agent.astream(
                input=agent_input,
                config=config_dict,
                stream_mode="messages",
            ):
                node_name = (
                    metadata.get("langgraph_node", "unknown")
                    if isinstance(metadata, dict)
                    else "unknown"
                )

                message_type = type(token).__name__

                if message_type in ("AIMessage", "AIMessageChunk"):
                    content_blocks = getattr(token, "content_blocks", None)
                    emitted_text = False

                    if content_blocks and isinstance(content_blocks, list):
                        for block in content_blocks:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text_content = block.get("text", "")
                                if text_content:
                                    yield {
                                        "type": "content",
                                        "data": text_content,
                                        "node": node_name,
                                    }
                                    emitted_text = True

                    if not emitted_text:
                        raw_content = getattr(token, "content", "")
                        if isinstance(raw_content, str) and raw_content:
                            yield {
                                "type": "content",
                                "data": raw_content,
                                "node": node_name,
                            }

            logger.info(f"[会话 {safe_session_id}] RAG Agent 查询完成（流式）")
            self._replace_latest_human_message(
                session_id=session_id,
                stored_question=question,
                display_question=history_question,
            )

            yield {"type": "complete"}

        except Exception as exc:
            logger.error(
                "[会话 {}] RAG Agent 查询失败（流式）: error_type={}",
                safe_session_id,
                type(exc).__name__,
            )
            raise

    async def query_stream_with_retrieval(
        self,
        question: str,
        session_id: str,
        metadata_filter: dict[str, Any] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a grounded answer and expose retrieval details before generation."""
        async with self._session_scope(session_id):
            async for event in self._query_stream_with_retrieval_locked(
                question,
                session_id,
                metadata_filter=metadata_filter,
            ):
                yield event

    async def _query_stream_with_retrieval_locked(
        self,
        question: str,
        session_id: str,
        metadata_filter: dict[str, Any] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Run one streaming RAG turn while the session lock is held."""
        retrieval_payload = await asyncio.to_thread(
            retrieve_structured_knowledge,
            question,
            metadata_filter=metadata_filter,
        )
        if not str(retrieval_payload.get("query") or "").strip():
            retrieval_payload = {**retrieval_payload, "query": question}
        retrieval_context = compact_retrieval_payload(retrieval_payload)

        if retrieval_payload.get("status") != "success":
            answer = build_no_answer_message(retrieval_payload)
            self._append_grounded_history(
                session_id,
                question,
                answer,
                assistant_metadata=_rag_history_metadata(
                    citations=[],
                    retrieval=retrieval_context,
                    no_answer=True,
                    answer_policy=retrieval_payload.get(
                        "answer_policy", "refuse_without_trusted_source"
                    ),
                ),
            )
            yield {
                "type": "search_results",
                "data": retrieval_context,
            }
            yield {
                "type": "content",
                "data": answer,
                "node": "retrieval_guard",
            }
            yield {
                "type": "complete",
                "data": {
                    "answer": answer,
                    "citations": [],
                    "retrieval": retrieval_context,
                    "no_answer": True,
                    "answer_policy": retrieval_payload.get(
                        "answer_policy", "refuse_without_trusted_source"
                    ),
                },
            }
            return

        preparation = prepare_grounded_generation(retrieval_payload)
        if preparation.refused:
            answer = preparation.refusal_answer
            guarded_context = preparation.refusal_context or retrieval_context
            self._append_grounded_history(
                session_id,
                question,
                answer,
                assistant_metadata=_rag_history_metadata(
                    citations=[],
                    retrieval=guarded_context,
                    no_answer=True,
                    answer_policy=preparation.refusal_policy,
                ),
            )
            yield {
                "type": "search_results",
                "data": guarded_context,
            }
            yield {
                "type": "content",
                "data": answer,
                "node": "citation_guard",
            }
            yield {
                "type": "complete",
                "data": {
                    "answer": answer,
                    "citations": [],
                    "retrieval": guarded_context,
                    "no_answer": True,
                    "answer_policy": preparation.refusal_policy,
                },
            }
            return

        generation_payload = cast(dict[str, Any], preparation.generation_payload)
        citations = preparation.citations
        yield {
            "type": "search_results",
            "data": retrieval_context,
        }
        grounded_question = build_grounded_question(question, generation_payload)
        full_answer = ""
        streamed_answer = ""
        async for chunk in self.query_grounded_stream(
            grounded_question,
            session_id,
            history_question=question,
        ):
            if chunk.get("type") == "content":
                full_answer += str(chunk.get("data") or "")
                validated_prefix = validated_citation_prefix(full_answer, citations)
                if len(validated_prefix) > len(streamed_answer):
                    delta = validated_prefix[len(streamed_answer) :]
                    streamed_answer = validated_prefix
                    yield {
                        "type": "content",
                        "data": delta,
                        "node": "citation_guard",
                    }
            elif chunk.get("type") == "complete":
                continue
            else:
                yield chunk

        decision, _repair_observability = await self._finalize_with_contract_repair(
            original_answer=full_answer,
            citations=citations,
            retrieval_payload=retrieval_payload,
            retrieval_context=retrieval_context,
            generation_payload=generation_payload,
            answer_contract=preparation.answer_contract,
            question=question,
            session_id=session_id,
            generation_observability={},
        )
        citations = decision.citations
        if decision.no_answer and decision.answer_policy == "refuse_without_citation":
            yield {
                "type": "replace_content" if streamed_answer else "content",
                "data": decision.answer,
                "node": "citation_guard",
            }
            self._append_grounded_history(
                session_id,
                question,
                decision.answer,
                assistant_metadata=_rag_history_metadata(
                    citations=[],
                    retrieval=decision.retrieval_context,
                    no_answer=True,
                    answer_policy=decision.answer_policy,
                ),
            )
            yield {
                "type": "complete",
                "data": {
                    "answer": decision.answer,
                    "citations": [],
                    "retrieval": decision.retrieval_context,
                    "no_answer": True,
                    "answer_policy": decision.answer_policy,
                },
            }
            return
        if decision.no_answer:
            answer = decision.answer
            self._append_grounded_history(
                session_id,
                question,
                answer,
                assistant_metadata=_rag_history_metadata(
                    citations=[],
                    retrieval=decision.retrieval_context,
                    no_answer=True,
                    answer_policy="refuse_without_trusted_source",
                ),
            )
            yield {
                "type": "replace_content" if streamed_answer else "content",
                "data": answer,
                "node": "retrieval_guard",
            }
            yield {
                "type": "complete",
                "data": {
                    "answer": answer,
                    "citations": [],
                    "retrieval": decision.retrieval_context,
                    "no_answer": True,
                    "answer_policy": decision.answer_policy,
                },
            }
            return

        final_answer = decision.answer
        if final_answer.startswith(streamed_answer):
            remaining_answer = final_answer[len(streamed_answer) :]
            if remaining_answer:
                yield {
                    "type": "content",
                    "data": remaining_answer,
                    "node": "citation_guard",
                }
        elif final_answer != streamed_answer:
            yield {
                "type": "replace_content",
                "data": final_answer,
                "node": "citation_guard",
            }
        self._append_grounded_history(
            session_id,
            question,
            final_answer,
            assistant_metadata=_rag_history_metadata(
                citations=citations,
                retrieval=decision.retrieval_context,
                no_answer=decision.no_answer,
                answer_policy=decision.answer_policy,
            ),
        )

        yield {
            "type": "complete",
            "data": {
                "answer": final_answer,
                "citations": citations,
                "retrieval": decision.retrieval_context,
                "no_answer": decision.no_answer,
                "answer_policy": decision.answer_policy,
            },
        }

    async def query_grounded_stream(
        self,
        grounded_question: str,
        session_id: str,
        *,
        history_question: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a grounded answer from the base model without Agent tools."""
        model = self._ensure_model()
        safe_session_id = sanitize_log_value(session_id)
        logger.info(f"[会话 {safe_session_id}] RAG grounded 流式生成（禁用工具）")
        messages = [
            SystemMessage(content=build_grounded_system_prompt()),
            HumanMessage(content=grounded_question),
        ]
        if not hasattr(model, "astream"):
            answer = await self.query_grounded(
                grounded_question,
                session_id,
                history_question=history_question,
            )
            if answer:
                yield {"type": "content", "data": answer, "node": "grounded_model"}
            yield {"type": "complete"}
            return

        async with self._model_stream_with_retry(
            model,
            messages,
            session_id=session_id,
        ) as chunks:
            async for text in chunks:
                yield {"type": "content", "data": text, "node": "grounded_model"}
        if history_question:
            logger.debug(
                f"[会话 {safe_session_id}] grounded 流式问答原始问题: "
                f"{summarize_text_for_log(history_question, label='question')}"
            )
        yield {"type": "complete"}

    async def get_session_history(self, session_id: str) -> list[dict[str, Any]]:
        """
        获取会话历史（从 MemorySaver checkpointer 中读取）

        Args:
            session_id: 会话ID（即 thread_id）

        Returns:
            list: 消息历史列表 [{"role": "user|assistant", "content": "...", "timestamp": "..."}]
        """
        safe_session_id = sanitize_log_value(session_id)
        async with self._session_scope(session_id):
            try:
                checkpoint_data = await self._aget_checkpoint_data(session_id)
                if not checkpoint_data:
                    grounded_history = list(self._grounded_history.get(session_id, []))
                    logger.info(
                        f"获取会话历史: {safe_session_id}, 消息数量: {len(grounded_history)}"
                    )
                    return grounded_history

                messages = checkpoint_data.get("channel_values", {}).get("messages", [])

                history: list[dict[str, Any]] = []
                for msg in messages:
                    if isinstance(msg, SystemMessage):
                        continue

                    role = "user" if isinstance(msg, HumanMessage) else "assistant"
                    content = message_content_to_text(
                        msg.content if hasattr(msg, "content") else msg
                    )
                    timestamp = str(getattr(msg, "timestamp", None) or datetime.now().isoformat())
                    history.append({"role": role, "content": content, "timestamp": timestamp})

                history.extend(self._grounded_history.get(session_id, []))
                history.sort(key=lambda item: item.get("timestamp", ""))
                logger.info(f"获取会话历史: {safe_session_id}, 消息数量: {len(history)}")
                return history

            except Exception as exc:
                logger.error(
                    "获取会话历史失败: session_id={}, error_type={}",
                    safe_session_id,
                    type(exc).__name__,
                )
                raise

    async def clear_session(self, session_id: str) -> bool:
        """
        清空会话历史（从 MemorySaver checkpointer 中删除）

        Args:
            session_id: 会话ID（即 thread_id）

        Returns:
            bool: 是否成功
        """
        async with self._session_scope(session_id):
            safe_session_id = sanitize_log_value(session_id)
            try:
                await self.checkpointer.adelete_thread(session_id)
                self._grounded_history.pop(session_id, None)
                logger.info(f"已清除会话历史: {safe_session_id}")
                return True
            except Exception as exc:
                logger.error(
                    "清空会话历史失败: session_id={}, error_type={}",
                    safe_session_id,
                    type(exc).__name__,
                )
                return False

    async def cleanup(self):
        """清理资源"""
        try:
            logger.info("清理 RAG Agent 服务资源...")
            # MCP 客户端由全局管理器统一管理，无需手动清理
            logger.info("RAG Agent 服务资源已清理")
        except Exception as exc:
            logger.error("清理资源失败: error_type={}", type(exc).__name__)

    def _require_agent(self) -> Any:
        """Return the initialized LangGraph agent or fail loudly."""
        if self.agent is None:
            raise RuntimeError("RAG Agent 未初始化")
        return self.agent

    def _get_checkpoint_data(self, session_id: str) -> dict[str, Any]:
        """Return the raw LangGraph checkpoint payload for a session."""
        config = {"configurable": {"thread_id": session_id}}
        checkpoint_tuple = self.checkpointer.get(cast(Any, config))
        if not checkpoint_tuple:
            return {}
        if isinstance(checkpoint_tuple, dict):
            checkpoint_data = checkpoint_tuple
        elif hasattr(checkpoint_tuple, "checkpoint"):
            checkpoint_data = getattr(checkpoint_tuple, "checkpoint", {})
        else:
            checkpoint_candidate = cast(Any, checkpoint_tuple)
            checkpoint_data = checkpoint_candidate[0] if checkpoint_candidate else {}
        return cast(dict[str, Any], checkpoint_data) if isinstance(checkpoint_data, dict) else {}

    async def _aget_checkpoint_data(self, session_id: str) -> dict[str, Any]:
        """Return the raw checkpoint payload without blocking the event loop."""
        config = {"configurable": {"thread_id": session_id}}
        checkpoint_data = await self.checkpointer.aget(cast(Any, config))
        return cast(dict[str, Any], checkpoint_data) if isinstance(checkpoint_data, dict) else {}

    def _append_grounded_history(
        self,
        session_id: str,
        question: str,
        answer: str,
        *,
        assistant_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist tool-free grounded RAG turns for the session-history API."""
        timestamp = datetime.now().isoformat()
        history = self._grounded_history.setdefault(session_id, [])
        history.extend(
            [
                {"role": "user", "content": question, "timestamp": timestamp},
                {
                    "role": "assistant",
                    "content": answer,
                    "timestamp": timestamp,
                    **(
                        {"metadata": dict(assistant_metadata)}
                        if assistant_metadata is not None
                        else {}
                    ),
                },
            ]
        )
        max_messages = int(config.rag_session_history_max_messages)
        if len(history) > max_messages:
            del history[:-max_messages]

    @asynccontextmanager
    async def _session_scope(self, session_id: str):
        """Serialize turns and cleanup for one session without blocking other sessions."""
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        self._session_lock_users[session_id] = self._session_lock_users.get(session_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._session_lock_users.get(session_id, 1) - 1
            if remaining <= 0:
                self._session_lock_users.pop(session_id, None)
                if not lock.locked():
                    self._session_locks.pop(session_id, None)
            else:
                self._session_lock_users[session_id] = remaining

    async def _invoke_model_with_retry(
        self,
        model: Any,
        messages: list[BaseMessage],
        *,
        session_id: str,
    ) -> Any:
        """Invoke the grounded model with one explicit timeout and bounded retry policy."""
        safe_session_id = sanitize_log_value(session_id)

        async def invoke() -> Any:
            return await model.ainvoke(messages)

        try:
            return await call_with_resilience(
                "llm",
                "rag_invoke",
                invoke,
                timeout_seconds=float(config.rag_model_timeout_seconds),
                max_attempts=int(config.rag_model_max_retries) + 1,
                retry_delay_seconds=float(config.rag_model_retry_delay_seconds),
                is_retryable=_is_retryable_model_error,
                failure_threshold=config.dependency_circuit_failure_threshold,
                recovery_timeout_seconds=config.dependency_circuit_recovery_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "RAG model call failed: session_id={}, error_type={}",
                safe_session_id,
                type(exc).__name__,
            )
            raise

    @asynccontextmanager
    async def _model_stream_with_retry(
        self,
        model: Any,
        messages: list[BaseMessage],
        *,
        session_id: str,
    ) -> AsyncIterator[AsyncIterator[str]]:
        """Stream one attempt, retrying only before any provider content is observed."""
        max_attempts = int(config.rag_model_max_retries) + 1
        safe_session_id = sanitize_log_value(session_id)

        async def stream_attempts() -> AsyncIterator[str]:
            breaker = get_circuit_breaker(
                "llm",
                failure_threshold=config.dependency_circuit_failure_threshold,
                recovery_timeout_seconds=config.dependency_circuit_recovery_seconds,
            )
            with dependency_operation("llm", "rag_stream") as observation:
                if not breaker.try_acquire_request():
                    observation.status = "circuit_open"
                    raise CircuitOpenError("llm circuit is open")
                deadline = asyncio.get_running_loop().time() + float(
                    config.rag_model_timeout_seconds
                )
                for attempt in range(1, max_attempts + 1):
                    emitted = False
                    emitted_bytes = 0
                    try:
                        remaining_timeout = deadline - asyncio.get_running_loop().time()
                        if remaining_timeout <= 0:
                            raise TimeoutError("RAG stream retry budget exhausted")
                        async with asyncio.timeout(remaining_timeout):
                            async for chunk in model.astream(messages):
                                content = chunk.content if hasattr(chunk, "content") else chunk
                                text = message_content_to_text(content)
                                if not text:
                                    continue
                                next_emitted_bytes = emitted_bytes + len(text.encode("utf-8"))
                                if next_emitted_bytes > int(
                                    config.rag_stream_spool_max_memory_bytes
                                ):
                                    raise ValueError("RAG 模型流输出超过安全上限")
                                emitted_bytes = next_emitted_bytes
                                emitted = True
                                yield text
                        breaker.record_success()
                        return
                    except asyncio.CancelledError:
                        breaker.release_request()
                        raise
                    except Exception as exc:
                        if emitted or attempt >= max_attempts or not _is_retryable_model_error(exc):
                            observation.status = "error"
                            if _is_retryable_model_error(exc):
                                breaker.record_failure()
                            else:
                                breaker.record_success()
                            raise
                        observation.retry_count += 1
                        logger.warning(
                            "RAG 模型流调用失败，准备重试: session_id={}, "
                            "attempt={}, error_type={}",
                            safe_session_id,
                            attempt,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(float(config.rag_model_retry_delay_seconds))
            raise RuntimeError("RAG 模型流未返回结果")

        yield stream_attempts()

    def _replace_latest_human_message(
        self,
        *,
        session_id: str,
        stored_question: str,
        display_question: str | None,
    ) -> None:
        """Best-effort replacement of an internal grounded prompt with the user's question."""
        if not display_question or display_question == stored_question:
            return

        try:
            checkpoint_data = self._get_checkpoint_data(session_id)
            channel_values = checkpoint_data.get("channel_values", {})
            messages = (
                channel_values.get("messages", []) if isinstance(channel_values, dict) else []
            )
            if not isinstance(messages, list):
                return

            for index in range(len(messages) - 1, -1, -1):
                msg = messages[index]
                if not isinstance(msg, HumanMessage):
                    continue
                if message_content_to_text(msg.content) != stored_question:
                    break
                messages[index] = copy_message_with_content(msg, display_question)
                logger.debug(
                    f"[会话 {sanitize_log_value(session_id)}] "
                    "已将 RAG grounded prompt 替换为用户原问题"
                )
                return
        except Exception as exc:
            logger.debug(
                "[会话 {}] 替换 RAG 会话问题失败: error_type={}",
                sanitize_log_value(session_id),
                type(exc).__name__,
            )

    def _ensure_model(self) -> Any:
        """Create the ChatQwen client lazily so imports and tests do not require cloud credentials."""
        if self.model is not None:
            return self.model
        if not config.dashscope_api_key or config.dashscope_api_key == "your-api-key-here":
            raise ValueError(
                "DASHSCOPE_API_KEY 未配置，无法调用 RAG 大模型。请在 .env 或环境变量中配置后重试。"
            )
        self.model = ChatQwen(
            model=self.model_name,
            api_key=cast(Any, config.dashscope_api_key),
            base_url=config.dashscope_api_base,
            temperature=0,
            streaming=self.streaming,
            timeout=float(config.rag_model_timeout_seconds),
            max_retries=0,
        )
        return self.model


rag_agent_service = RagAgentService(streaming=True)


def _is_retryable_model_error(exc: Exception) -> bool:
    """Return True for transient provider/network failures only."""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    error_name = type(exc).__name__.lower()
    message = str(exc).lower()
    retryable_markers = (
        "timeout",
        "timed out",
        "connection",
        "rate limit",
        "ratelimit",
        "temporarily unavailable",
        "service unavailable",
        "429",
        "500",
        "502",
        "503",
        "504",
    )
    return any(marker in error_name or marker in message for marker in retryable_markers)


def extract_message_token_usage(message: Any) -> dict[str, Any] | None:
    """Normalize LangChain/OpenAI-compatible usage metadata without estimating tokens."""
    candidates = [
        getattr(message, "usage_metadata", None),
        getattr(message, "response_metadata", None),
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        usage = candidate.get("token_usage") or candidate.get("usage") or candidate
        if not isinstance(usage, dict):
            continue
        input_tokens = _optional_int(usage.get("input_tokens", usage.get("prompt_tokens")))
        output_tokens = _optional_int(usage.get("output_tokens", usage.get("completion_tokens")))
        total_tokens = _optional_int(usage.get("total_tokens"))
        if input_tokens is None and output_tokens is None and total_tokens is None:
            continue
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        return {
            "status": "observed",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
    return None


def _build_contract_repair_prompt(
    generation_payload: dict[str, Any],
    original_answer: str,
    violations: tuple[ContractViolation, ...],
    contract: AnswerContract,
) -> str:
    """Render one repair prompt from frozen context and stable contract metadata."""
    slots_by_id = {slot.subgoal_id: slot for slot in contract.slots}
    all_allowed = tuple(
        dict.fromkeys(
            index for slot in contract.slots for index in slot.allowed_citation_indices
        )
    )
    violation_lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for violation in violations:
        identity = (violation.code, violation.subgoal_id)
        if identity in seen:
            continue
        seen.add(identity)
        slot = slots_by_id.get(violation.subgoal_id)
        allowed = slot.allowed_citation_indices if slot is not None else all_allowed
        allowed_text = ",".join(str(index) for index in allowed) or "none"
        violation_lines.append(
            f"- code={violation.code}; affected_slot={violation.subgoal_id or 'all'}; "
            f"allowed_evidence={allowed_text}"
        )
    violations_text = "\n".join(violation_lines)
    return (
        "冻结证据（不得新增或替换）:\n"
        f"{build_generation_context(generation_payload)}\n\n"
        "原始答案:\n"
        f"{original_answer}\n\n"
        "契约违规:\n"
        f"{violations_text}\n\n"
        "只输出修复后的要点；每条事实只绑定一个允许的 [证据 N]。"
    )


def build_rag_observability(
    retrieval_payload: dict[str, Any],
    generation: dict[str, Any],
    *,
    total_ms: float,
) -> dict[str, Any]:
    """Combine retrieval and generation observations for API and benchmark reuse."""
    retrieval = dict(retrieval_payload.get("observability") or {})
    stages = dict(retrieval.get("stages") or {})
    stages.update(
        {
            "context_build_ms": "not_observed",
            "llm_ttft_ms": generation.get("llm_ttft_ms", "not_observed"),
            "llm_generation_ms": generation.get("llm_generation_ms", 0.0),
            "total_ms": round(total_ms, 2),
        }
    )
    runtime = dict(retrieval.get("runtime") or {})
    runtime["llm_model"] = generation.get("model", config.effective_rag_model)
    return {
        "stages": stages,
        "counts": retrieval.get("counts", {}),
        "runtime": runtime,
        "token_usage": generation.get("token_usage", {"status": "not_observed"}),
        "estimated_cost": {
            "status": "not_observed",
            "amount": None,
            "currency": None,
            "price_snapshot": None,
        },
        "limitations": retrieval.get("limitations", []),
        "generation_repair": {
            "attempted": bool(generation.get("citation_repair_retry")),
            "reason": generation.get("repair_reason", ""),
            "sources": list(generation.get("repair_sources") or []),
        },
    }


def _merge_generation_observability(
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, Any]:
    """Combine one citation-repair retry without hiding the extra model cost."""
    merged = dict(second)
    merged["llm_generation_ms"] = round(
        float(first.get("llm_generation_ms") or 0.0)
        + float(second.get("llm_generation_ms") or 0.0),
        2,
    )
    first_usage = first.get("token_usage")
    second_usage = second.get("token_usage")
    if isinstance(first_usage, dict) and isinstance(second_usage, dict):
        totals = {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            values = [first_usage.get(key), second_usage.get(key)]
            totals[key] = (
                sum(int(value) for value in values if value is not None)
                if any(value is not None for value in values)
                else None
            )
        merged["token_usage"] = {"status": "observed", **totals}
    merged["citation_repair_retry"] = True
    return merged


def _rag_history_metadata(
    *,
    citations: list[dict[str, Any]],
    retrieval: dict[str, Any],
    no_answer: bool,
    answer_policy: str,
) -> dict[str, Any]:
    """Build the frontend metadata persisted with one assistant history item."""
    return {
        "citations": [dict(item) for item in citations],
        "retrieval": dict(retrieval),
        "noAnswer": no_answer,
        "answerPolicy": answer_policy,
    }


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
