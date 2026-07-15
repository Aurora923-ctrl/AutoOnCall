"""RAG Agent 服务 - 基于 LangGraph 的智能代理

使用 langchain_qwq 的 ChatQwen 原生集成，
支持真正的流式输出和更好的模型适配。
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Sequence
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

from app.agent.mcp_client import get_mcp_client_with_retry
from app.config import config
from app.services.rag_answer_policy import (
    build_citation_guard_payload,
    build_grounded_question,
    build_grounded_system_prompt,
    build_missing_citation_message,
    build_no_answer_message,
    copy_message_with_content,
    ensure_citation_block,
    has_valid_citations,
    is_explicit_knowledge_refusal,
    message_content_to_text,
    select_supporting_citations,
)
from app.services.rag_read_models import (
    build_citations,
    compact_retrieval_payload,
)
from app.services.rag_retrieval_service import (
    retrieve_structured_knowledge,
)
from app.tools import get_current_time, retrieve_knowledge
from app.utils.log_safety import summarize_text_for_log

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
        self._grounded_history: dict[str, list[dict[str, str]]] = {}

        self.agent: Any | None = None
        self._agent_initialized = False

        logger.info(f"RAG Agent 服务初始化完成, model={self.model_name}, streaming={streaming}")

    async def _initialize_agent(self):
        """异步初始化 Agent（包括 MCP 工具）"""
        if self._agent_initialized:
            return

        model = self._ensure_model()
        try:
            mcp_client = await get_mcp_client_with_retry()
            mcp_tools = await mcp_client.get_tools()
            logger.info(f"成功加载 {len(mcp_tools)} 个 MCP 工具")
        except Exception as e:
            logger.warning(f"MCP 工具加载失败，RAG Agent 将仅使用本地工具: {e}")
            mcp_tools = []

        # 将 MCP 工具添加到实例变量中
        self.mcp_tools = mcp_tools

        all_tools = self.tools + self.mcp_tools

        self.agent = create_agent(
            model,
            tools=all_tools,
            checkpointer=self.checkpointer,
        )

        self._agent_initialized = True

        if all_tools:
            tool_names = [tool.name if hasattr(tool, "name") else str(tool) for tool in all_tools]
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
        try:
            await self._initialize_agent()
            agent = self._require_agent()

            logger.info(
                f"[会话 {session_id}] RAG Agent 收到查询（非流式）: "
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
                    logger.info(f"[会话 {session_id}] Agent 调用了工具: {tool_names}")

                logger.info(f"[会话 {session_id}] RAG Agent 查询完成（非流式）")
                return answer

            logger.warning(f"[会话 {session_id}] Agent 返回结果为空")
            return ""

        except Exception as e:
            logger.error(f"[会话 {session_id}] RAG Agent 查询失败（非流式）: {e}")
            raise

    async def query_with_retrieval(
        self,
        question: str,
        session_id: str,
        metadata_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Answer a knowledge-base question with explicit retrieval citations."""
        retrieval_payload = await asyncio.to_thread(
            retrieve_structured_knowledge,
            question,
            metadata_filter=metadata_filter,
        )
        retrieval_context = compact_retrieval_payload(retrieval_payload)

        if retrieval_payload.get("status") != "success":
            answer = build_no_answer_message(retrieval_payload)
            self._append_grounded_history(session_id, question, answer)
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

        citations = build_citations(retrieval_payload)
        if not has_valid_citations(citations):
            answer = build_missing_citation_message()
            guarded_payload = build_citation_guard_payload(retrieval_payload)
            self._append_grounded_history(session_id, question, answer)
            return {
                "success": True,
                "answer": answer,
                "citations": [],
                "retrieval": compact_retrieval_payload(guarded_payload),
                "no_answer": True,
                "answer_policy": "refuse_without_citation",
            }

        grounded_question = build_grounded_question(question, retrieval_payload)
        generation_started = time.perf_counter()
        answer, generation_observability = await self.query_grounded_observed(
            grounded_question,
            session_id,
            history_question=question,
        )
        if is_explicit_knowledge_refusal(answer):
            answer = build_no_answer_message(
                {
                    **retrieval_payload,
                    "status": "no_answer",
                    "summary": "当前知识库没有足够的相关证据回答该问题。",
                }
            )
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
            self._append_grounded_history(session_id, question, answer)
            return {
                "success": True,
                "answer": answer,
                "citations": [],
                "retrieval": retrieval_context,
                "no_answer": True,
                "answer_policy": "refuse_without_trusted_source",
                "observability": observability,
            }
        citations = select_supporting_citations(answer, citations)
        if not has_valid_citations(citations):
            answer = build_missing_citation_message()
            guarded_payload = build_citation_guard_payload(retrieval_payload)
            self._append_grounded_history(session_id, question, answer)
            return {
                "success": True,
                "answer": answer,
                "citations": [],
                "retrieval": compact_retrieval_payload(guarded_payload),
                "no_answer": True,
                "answer_policy": "refuse_without_citation",
                "observability": build_rag_observability(
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
                ),
            }
        answer = ensure_citation_block(answer, citations)
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
        self._append_grounded_history(session_id, question, answer)

        return {
            "success": True,
            "answer": answer,
            "citations": citations,
            "retrieval": retrieval_context,
            "no_answer": False,
            "answer_policy": retrieval_payload.get("answer_policy", "answer_with_citations"),
            "observability": observability,
        }

    async def query_grounded(
        self,
        grounded_question: str,
        session_id: str,
        *,
        history_question: str | None = None,
    ) -> str:
        """Generate a grounded answer without exposing any Agent tools to the model."""
        model = self._ensure_model()
        logger.info(f"[会话 {session_id}] RAG grounded 生成（禁用工具）")
        messages = [
            SystemMessage(content=build_grounded_system_prompt()),
            HumanMessage(content=grounded_question),
        ]
        result = await model.ainvoke(messages)
        content = result.content if hasattr(result, "content") else result
        answer = message_content_to_text(content)
        if history_question:
            logger.debug(
                f"[会话 {session_id}] grounded 问答原始问题: "
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
        result = await model.ainvoke(messages)
        content = result.content if hasattr(result, "content") else result
        answer = message_content_to_text(content)
        usage = extract_message_token_usage(result)
        return answer, {
            "llm_generation_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "llm_ttft_ms": "not_observed",
            "token_usage": usage or {
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
        try:
            await self._initialize_agent()
            agent = self._require_agent()

            logger.info(
                f"[会话 {session_id}] RAG Agent 收到查询（流式）: "
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

            logger.info(f"[会话 {session_id}] RAG Agent 查询完成（流式）")
            self._replace_latest_human_message(
                session_id=session_id,
                stored_question=question,
                display_question=history_question,
            )

            yield {"type": "complete"}

        except Exception as e:
            logger.error(f"[会话 {session_id}] RAG Agent 查询失败（流式）: {e}")
            raise

    async def query_stream_with_retrieval(
        self,
        question: str,
        session_id: str,
        metadata_filter: dict[str, Any] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a grounded answer and expose retrieval details before generation."""
        retrieval_payload = await asyncio.to_thread(
            retrieve_structured_knowledge,
            question,
            metadata_filter=metadata_filter,
        )
        retrieval_context = compact_retrieval_payload(retrieval_payload)

        if retrieval_payload.get("status") != "success":
            answer = build_no_answer_message(retrieval_payload)
            self._append_grounded_history(session_id, question, answer)
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

        citations = build_citations(retrieval_payload)
        if not has_valid_citations(citations):
            answer = build_missing_citation_message()
            guarded_payload = build_citation_guard_payload(retrieval_payload)
            guarded_context = compact_retrieval_payload(guarded_payload)
            self._append_grounded_history(session_id, question, answer)
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
                    "answer_policy": "refuse_without_citation",
                },
            }
            return

        yield {
            "type": "search_results",
            "data": retrieval_context,
        }
        grounded_question = build_grounded_question(question, retrieval_payload)
        full_answer = ""
        async for chunk in self.query_grounded_stream(
            grounded_question,
            session_id,
            history_question=question,
        ):
            if chunk.get("type") == "content":
                full_answer += str(chunk.get("data") or "")
                yield chunk
            elif chunk.get("type") == "complete":
                continue
            else:
                yield chunk

        if is_explicit_knowledge_refusal(full_answer):
            answer = build_no_answer_message(
                {
                    **retrieval_payload,
                    "status": "no_answer",
                    "summary": "当前知识库没有足够的相关证据回答该问题。",
                }
            )
            self._append_grounded_history(session_id, question, answer)
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
                    "answer_policy": "refuse_without_trusted_source",
                },
            }
            return

        citations = select_supporting_citations(full_answer, citations)
        if not has_valid_citations(citations):
            answer = build_missing_citation_message()
            guarded_payload = build_citation_guard_payload(retrieval_payload)
            guarded_context = compact_retrieval_payload(guarded_payload)
            yield {
                "type": "content",
                "data": answer,
                "node": "citation_guard",
            }
            self._append_grounded_history(session_id, question, answer)
            yield {
                "type": "complete",
                "data": {
                    "answer": answer,
                    "citations": [],
                    "retrieval": guarded_context,
                    "no_answer": True,
                    "answer_policy": "refuse_without_citation",
                },
            }
            return
        final_answer = ensure_citation_block(full_answer, citations)
        appended_content = final_answer[len(full_answer) :]
        if appended_content:
            yield {
                "type": "content",
                "data": appended_content,
                "node": "citation_guard",
            }
        self._append_grounded_history(session_id, question, final_answer)

        yield {
            "type": "complete",
            "data": {
                "answer": final_answer,
                "citations": citations,
                "retrieval": retrieval_context,
                "no_answer": False,
                "answer_policy": retrieval_payload.get("answer_policy", "answer_with_citations"),
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
        logger.info(f"[会话 {session_id}] RAG grounded 流式生成（禁用工具）")
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

        async for chunk in model.astream(messages):
            content = chunk.content if hasattr(chunk, "content") else chunk
            text = message_content_to_text(content)
            if text:
                yield {"type": "content", "data": text, "node": "grounded_model"}
        if history_question:
            logger.debug(
                f"[会话 {session_id}] grounded 流式问答原始问题: "
                f"{summarize_text_for_log(history_question, label='question')}"
            )
        yield {"type": "complete"}

    def get_session_history(self, session_id: str) -> list:
        """
        获取会话历史（从 MemorySaver checkpointer 中读取）

        Args:
            session_id: 会话ID（即 thread_id）

        Returns:
            list: 消息历史列表 [{"role": "user|assistant", "content": "...", "timestamp": "..."}]
        """
        try:
            checkpoint_data = self._get_checkpoint_data(session_id)
            if not checkpoint_data:
                grounded_history = list(self._grounded_history.get(session_id, []))
                logger.info(f"获取会话历史: {session_id}, 消息数量: {len(grounded_history)}")
                return grounded_history

            messages = checkpoint_data.get("channel_values", {}).get("messages", [])

            # 转换为前端需要的格式
            history = []
            for msg in messages:
                if isinstance(msg, SystemMessage):
                    continue

                role = "user" if isinstance(msg, HumanMessage) else "assistant"
                content = msg.content if hasattr(msg, "content") else str(msg)

                timestamp = getattr(msg, "timestamp", None)
                if timestamp:
                    history.append({"role": role, "content": content, "timestamp": timestamp})
                else:
                    from datetime import datetime

                    history.append(
                        {"role": role, "content": content, "timestamp": datetime.now().isoformat()}
                    )

            history.extend(self._grounded_history.get(session_id, []))
            history.sort(key=lambda item: item.get("timestamp", ""))
            logger.info(f"获取会话历史: {session_id}, 消息数量: {len(history)}")
            return history

        except Exception as e:
            logger.error(f"获取会话历史失败: {session_id}, 错误: {e}")
            return []

    def clear_session(self, session_id: str) -> bool:
        """
        清空会话历史（从 MemorySaver checkpointer 中删除）

        Args:
            session_id: 会话ID（即 thread_id）

        Returns:
            bool: 是否成功
        """
        try:
            self.checkpointer.delete_thread(session_id)
            self._grounded_history.pop(session_id, None)

            logger.info(f"已清除会话历史: {session_id}")
            return True

        except Exception as e:
            logger.error(f"清空会话历史失败: {session_id}, 错误: {e}")
            return False

    async def cleanup(self):
        """清理资源"""
        try:
            logger.info("清理 RAG Agent 服务资源...")
            # MCP 客户端由全局管理器统一管理，无需手动清理
            logger.info("RAG Agent 服务资源已清理")
        except Exception as e:
            logger.error(f"清理资源失败: {e}")

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

    def _append_grounded_history(self, session_id: str, question: str, answer: str) -> None:
        """Persist tool-free grounded RAG turns for the session-history API."""
        timestamp = datetime.now().isoformat()
        self._grounded_history.setdefault(session_id, []).extend(
            [
                {"role": "user", "content": question, "timestamp": timestamp},
                {"role": "assistant", "content": answer, "timestamp": timestamp},
            ]
        )

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
                logger.debug(f"[会话 {session_id}] 已将 RAG grounded prompt 替换为用户原问题")
                return
        except Exception as e:
            logger.debug(f"[会话 {session_id}] 替换 RAG 会话问题失败: {e}")

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
        )
        return self.model


rag_agent_service = RagAgentService(streaming=True)


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
        input_tokens = _optional_int(
            usage.get("input_tokens", usage.get("prompt_tokens"))
        )
        output_tokens = _optional_int(
            usage.get("output_tokens", usage.get("completion_tokens"))
        )
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
    }


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
