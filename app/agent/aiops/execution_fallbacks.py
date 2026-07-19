"""Legacy fallback execution paths for AIOps executor."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_qwq import ChatQwen
from langgraph.prebuilt import ToolNode
from loguru import logger

from app.config import config
from app.models.plan import PlanStep
from app.tools.base import (
    ToolExecutionResult,
    extract_tool_error_message,
    is_failed_tool_output,
)


@dataclass
class FallbackExecutionOutcome:
    """Final fallback text plus the real safe tools invoked by ToolNode."""

    text: str
    tool_results: list[ToolExecutionResult] = field(default_factory=list)


async def execute_with_llm_tools(task: str, all_tools: list[Any]) -> FallbackExecutionOutcome:
    """Execute a task through the legacy LLM + ToolNode path."""
    llm = ChatQwen(
        model=config.effective_rag_model,
        api_key=cast(Any, config.dashscope_api_key),
        base_url=config.dashscope_api_base,
        temperature=0,
    )

    safe_tools = safe_fallback_tools(all_tools)
    llm_with_tools = llm.bind_tools(safe_tools) if safe_tools else llm
    tool_node = ToolNode(safe_tools) if safe_tools else None

    messages = [
        SystemMessage(
            content="""你是一个能力强大的助手，负责执行具体的任务步骤。

你可以使用各种工具来完成任务。对于每个步骤：
1. 理解步骤的目标
2. 如果步骤指定了工具名，优先使用该工具
3. 调用工具获取信息
4. 返回执行结果

注意：
- 如果工具调用失败，请说明失败原因
- 不要编造数据，只返回实际获取的信息
- 执行结果要清晰、准确
- 专注于当前步骤，不要考虑其他任务"""
        ),
        HumanMessage(content=f"请执行以下任务: {task}"),
    ]

    llm_response = await llm_with_tools.ainvoke(messages)
    logger.info(f"LLM 响应类型: {type(llm_response)}")

    if tool_node is not None and hasattr(llm_response, "tool_calls") and llm_response.tool_calls:
        logger.info(f"检测到 {len(llm_response.tool_calls)} 个工具调用")
        messages.append(llm_response)
        tool_started_at = time.perf_counter()
        tool_messages = await tool_node.ainvoke({"messages": messages})
        tool_latency_ms = round((time.perf_counter() - tool_started_at) * 1000, 3)
        messages.extend(tool_messages["messages"])
        final_response = await llm_with_tools.ainvoke(messages)
        return FallbackExecutionOutcome(
            text=message_content_to_text(
                final_response.content if hasattr(final_response, "content") else final_response
            ),
            tool_results=_fallback_tool_results(
                list(llm_response.tool_calls),
                list(tool_messages.get("messages") or []),
                latency_ms=tool_latency_ms,
            ),
        )

    logger.info("LLM 未调用工具，直接返回结果")
    return FallbackExecutionOutcome(
        text=message_content_to_text(
            llm_response.content if hasattr(llm_response, "content") else llm_response
        )
    )


def message_content_to_text(content: Any) -> str:
    """Render LangChain message content into a stable text result."""
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


def safe_fallback_tools(all_tools: list[Any]) -> list[Any]:
    """Limit legacy LLM fallback to explicitly safe local read-only helpers."""
    safe_names = {"get_current_time", "retrieve_knowledge"}
    return [
        tool
        for tool in all_tools
        if getattr(tool, "name", getattr(tool, "__name__", "")) in safe_names
    ]


def _fallback_tool_results(
    tool_calls: list[Any],
    tool_messages: list[Any],
    *,
    latency_ms: float,
) -> list[ToolExecutionResult]:
    """Normalize actual ToolNode calls without pretending the planned tool ran."""
    messages_by_call_id = {
        str(getattr(message, "tool_call_id", "") or ""): message
        for message in tool_messages
        if getattr(message, "tool_call_id", None)
    }
    results: list[ToolExecutionResult] = []
    for index, call in enumerate(tool_calls, 1):
        call_data = call if isinstance(call, dict) else {}
        call_id = str(call_data.get("id") or f"fallback-call-{index}")
        tool_name = str(call_data.get("name") or "unknown_fallback_tool")
        input_args = call_data.get("args")
        if not isinstance(input_args, dict):
            input_args = {}
        message = messages_by_call_id.get(call_id)
        raw_content = getattr(message, "content", None) if message is not None else None
        output = _parse_tool_message_content(raw_content)
        message_status = str(getattr(message, "status", "") or "").lower()
        failed = (
            message is None
            or message_status in {"error", "failed"}
            or is_failed_tool_output(output)
        )
        error_message = (
            "ToolNode did not return a matching ToolMessage"
            if message is None
            else extract_tool_error_message(output)
            if failed
            else None
        )
        results.append(
            ToolExecutionResult(
                tool_name=tool_name,
                status="failed" if failed else "success",
                input_args=input_args,
                output=output,
                latency_ms=latency_ms,
                risk_level="low",
                read_only=True,
                error_message=error_message,
                metadata={
                    "execution_path": "llm_toolnode_fallback",
                    "invocation_kind": "tool",
                    "actual_tool_invoked": True,
                    "fallback_tool_call_id": call_id,
                    "latency_scope": "toolnode_batch",
                },
            )
        )
    return results


def _parse_tool_message_content(content: Any) -> Any:
    if not isinstance(content, str):
        return content
    text = content.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def ensure_plan_step(plan_step: PlanStep | None, task: str) -> PlanStep:
    """Create a minimal PlanStep for legacy string-only plan entries."""
    if plan_step:
        return plan_step
    return PlanStep(
        step_id="legacy-step",
        tool_name="legacy_plan_step",
        purpose=task,
        input_args={"task": task},
        expected_evidence="Legacy execution result wrapped as structured evidence",
        risk_level="low",
        status="pending",
    )


def fallback_text_to_tool_result(
    task: str,
    result_text: str,
    plan_step: PlanStep,
) -> ToolExecutionResult:
    """Wrap manual or unregistered fallback execution in the standard tool result shape."""
    tool_name = plan_step.tool_name
    input_args = plan_step.input_args
    execution_path = (
        "manual_analysis" if plan_step.tool_name == "manual_analysis" else "llm_toolnode_fallback"
    )
    fallback_reason = (
        "manual_analysis_requested"
        if execution_path == "manual_analysis"
        else "structured_tool_not_registered"
    )
    is_manual_analysis = execution_path == "manual_analysis"
    error_message = None
    if not is_manual_analysis:
        error_message = (
            f"工具 {tool_name} 未注册到 Tool Registry，"
            "LLM ToolNode 兜底结果不可作为标准工具成功证据。"
        )
    return ToolExecutionResult(
        tool_name=tool_name,
        status="success" if is_manual_analysis else "failed",
        input_args=input_args,
        output={
            "summary": result_text,
            "task": task,
            "execution_path": execution_path,
            "structured_tool_registered": False,
            "fallback_reason": fallback_reason,
        },
        risk_level=plan_step.risk_level,
        read_only=True,
        error_message=error_message,
        metadata={
            "execution_path": execution_path,
            "structured_tool_registered": False,
            "fallback_reason": fallback_reason,
            "invocation_kind": "analysis_fallback",
            "actual_tool_invoked": False,
        },
    )


def fallback_warnings(
    tool_call_record: dict[str, Any],
    plan_step: PlanStep | None,
) -> list[str]:
    """Return operator-visible warnings for non-registry execution paths."""
    output = tool_call_record.get("output") if isinstance(tool_call_record, dict) else {}
    if not isinstance(output, dict):
        return []
    execution_path = str(output.get("execution_path") or "")
    if execution_path == "llm_toolnode_fallback":
        tool_name = tool_call_record.get("tool_name") or getattr(plan_step, "tool_name", "unknown")
        step_id = tool_call_record.get("step_id") or getattr(plan_step, "step_id", "unknown")
        return [
            (
                f"步骤 {step_id} 使用了 LLM ToolNode 兜底路径："
                f"工具 {tool_name} 未注册到 Tool Registry，结果需用标准工具复核。"
            )
        ]
    if execution_path == "manual_analysis":
        step_id = tool_call_record.get("step_id") or getattr(plan_step, "step_id", "unknown")
        return [f"步骤 {step_id} 使用人工分析兜底路径，结论需结合真实工具证据复核。"]
    return []
