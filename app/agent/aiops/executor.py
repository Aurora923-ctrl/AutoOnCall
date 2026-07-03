"""
Executor 节点：执行单个步骤
基于 LangGraph 官方教程实现
"""

import json
import re
from typing import Any, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_qwq import ChatQwen
from langgraph.prebuilt import ToolNode
from loguru import logger

from app.agent.mcp_client import get_mcp_client_with_retry
from app.config import config
from app.models.approval import ApprovalRequest
from app.models.evidence import (
    Evidence,
    build_confidence_reason,
    infer_evidence_stance,
    infer_evidence_type,
    normalize_data_source,
)
from app.models.plan import PlanStep
from app.models.trace import ToolCallRecord
from app.services.approval_service import approval_service
from app.services.approval_workflow import create_approval_request_from_risk_decision
from app.services.trace_service import trace_service
from app.tools import get_current_time, retrieve_knowledge
from app.tools.base import ToolExecutionResult
from app.tools.registry import ToolRegistry, create_default_tool_registry

from .risk_controller import RiskControlDecision, assess_plan_step
from .state import (
    PlanExecuteState,
    normalize_plan_state_update,
    parse_plan_step,
    remaining_plan_state_update,
)


async def executor(state: PlanExecuteState) -> dict[str, Any]:
    """
    执行节点：执行计划中的下一个步骤

    使用 LangGraph 的 ToolNode 自动处理工具调用
    """
    logger.info("=== Executor：执行步骤 ===")

    current_plan = state.get("current_plan", [])
    plan = state.get("plan", [])

    if not current_plan and not plan:
        logger.info("计划为空，跳过执行")
        return {}

    plan_step = _get_current_plan_step(current_plan)
    task = _format_plan_step_for_execution(plan_step) if plan_step else plan[0]
    logger.info(f"当前任务: {task}")

    try:
        local_tools = [get_current_time, retrieve_knowledge]

        mcp_tools = []
        try:
            mcp_client = await get_mcp_client_with_retry()
            mcp_tools = await mcp_client.get_tools()
        except Exception as exc:
            logger.warning(f"获取 MCP 工具失败，将继续使用本地和 mock 工具: {exc}")
        logger.info(f"可用工具数量: 本地 {len(local_tools)} + MCP {len(mcp_tools)}")

        all_tools = local_tools + mcp_tools

        registry = create_default_tool_registry(all_tools)
        logger.info(f"Tool Registry 已加载 {len(registry.list_tools())} 个标准工具")

        risk_block_update = _risk_gate_state_update(
            plan_step, registry, state, current_plan, plan, task
        )
        if risk_block_update is not None:
            return risk_block_update

        direct_result = await _try_execute_registered_step(plan_step, registry, state)
        gathered_evidence: list[dict[str, Any]] = []
        tool_call_records: list[dict[str, Any]] = []
        errors: list[str] = []
        warnings: list[str] = []

        if direct_result is not None:
            result, step_status, evidence, tool_call_record = direct_result
            gathered_evidence.append(evidence)
            tool_call_records.append(tool_call_record)
            if step_status == "failed":
                errors.append(_format_tool_error(tool_call_record))
        else:
            result, step_status, evidence, tool_call_record = await _execute_fallback_step(
                task,
                plan_step,
                all_tools,
                state,
            )
            gathered_evidence.append(evidence)
            tool_call_records.append(tool_call_record)
            warnings.extend(_fallback_warnings(tool_call_record, plan_step))
            if step_status == "failed":
                errors.append(_format_tool_error(tool_call_record))

        logger.info(f"步骤执行完成，结果长度: {len(result)}")

        executed_step = _mark_step(plan_step, step_status) if plan_step else None

        state_update = {
            **remaining_plan_state_update(current_plan, plan),
            "past_steps": [(task, result)],  # 使用 operator.add 追加
            "executed_steps": [executed_step] if executed_step else [],
        }
        if gathered_evidence:
            state_update["gathered_evidence"] = gathered_evidence
        if tool_call_records:
            state_update["tool_call_records"] = tool_call_records
        if errors:
            state_update["errors"] = errors
        if warnings:
            state_update["warnings"] = warnings
        return state_update

    except Exception as e:
        logger.error(f"执行步骤失败: {e}", exc_info=True)
        executed_step = _mark_step(plan_step, "failed") if plan_step else None
        state_update = {
            **remaining_plan_state_update(current_plan, plan),
            "past_steps": [(task, f"执行失败: {str(e)}")],
            "executed_steps": [executed_step] if executed_step else [],
            "errors": [f"步骤 {task} 执行失败: {str(e)}"],
        }
        if plan_step:
            failed_result = ToolExecutionResult(
                tool_name=plan_step.tool_name,
                status="failed",
                input_args=plan_step.input_args,
                error_message=str(e),
            )
            persisted_result = _result_for_persistence(failed_result)
            state_update["gathered_evidence"] = [
                _tool_result_to_evidence(persisted_result, plan_step).model_dump(mode="json")
            ]
            state_update["tool_call_records"] = [
                _record_and_dump_tool_call(persisted_result, plan_step, state)
            ]
        return state_update


async def _execute_with_llm_tools(task: str, all_tools: list) -> str:
    """Execute a task through the legacy LLM + ToolNode path."""
    # 创建 LLM（绑定工具）
    llm = ChatQwen(
        model=config.effective_rag_model,
        api_key=cast(Any, config.dashscope_api_key),
        base_url=config.dashscope_api_base,
        temperature=0,
    )

    safe_tools = _safe_fallback_tools(all_tools)
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
        tool_messages = await tool_node.ainvoke({"messages": messages})
        messages.extend(tool_messages["messages"])
        final_response = await llm_with_tools.ainvoke(messages)
        return _message_content_to_text(
            final_response.content if hasattr(final_response, "content") else final_response
        )

    logger.info("LLM 未调用工具，直接返回结果")
    return _message_content_to_text(
        llm_response.content if hasattr(llm_response, "content") else llm_response
    )


def _message_content_to_text(content: Any) -> str:
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


def _safe_fallback_tools(all_tools: list[Any]) -> list[Any]:
    """Limit legacy LLM fallback to explicitly safe local read-only helpers."""
    safe_names = {"get_current_time", "retrieve_knowledge"}
    return [
        tool
        for tool in all_tools
        if getattr(tool, "name", getattr(tool, "__name__", "")) in safe_names
    ]


async def _try_execute_registered_step(
    plan_step: PlanStep | None,
    registry: ToolRegistry,
    state: PlanExecuteState,
) -> tuple[str, str, dict[str, Any], dict[str, Any]] | None:
    """Try deterministic execution through the Tool Registry."""
    if not plan_step or plan_step.tool_name == "manual_analysis":
        return None

    if not registry.get(plan_step.tool_name):
        logger.info(f"结构化工具 {plan_step.tool_name} 未注册，回退到 LLM 工具执行")
        return None

    logger.info(
        f"通过 Tool Registry 调用 {plan_step.tool_name}: "
        f"{_summarize_input_args(plan_step.input_args)}"
    )
    result = await registry.arun(plan_step.tool_name, plan_step.input_args)
    persisted_result = _result_for_persistence(result)
    evidence = _tool_result_to_evidence(persisted_result, plan_step)
    tool_call_record = _tool_result_to_call_record(persisted_result, plan_step, state)
    trace_service.record_tool_call(tool_call_record)
    return (
        json.dumps(
            persisted_result.model_dump(mode="json"),
            ensure_ascii=False,
            default=str,
            indent=2,
        ),
        "success" if result.status == "success" else "failed",
        evidence.model_dump(mode="json"),
        tool_call_record.model_dump(mode="json"),
    )


async def _execute_fallback_step(
    task: str,
    plan_step: PlanStep | None,
    all_tools: list,
    state: PlanExecuteState,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Execute the legacy fallback path and normalize it into evidence records."""
    normalized_step = _ensure_plan_step(plan_step, task)
    result_text = await _execute_with_llm_tools(task, all_tools)
    result = _fallback_text_to_tool_result(task, result_text, normalized_step)
    evidence = _tool_result_to_evidence(result, normalized_step)
    tool_call_record = _record_and_dump_tool_call(result, normalized_step, state)
    return result_text, result.status, evidence.model_dump(mode="json"), tool_call_record


def _ensure_plan_step(plan_step: PlanStep | None, task: str) -> PlanStep:
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


def _fallback_text_to_tool_result(
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
        },
    )


def _fallback_warnings(
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


def _risk_gate_state_update(
    plan_step: PlanStep | None,
    registry: ToolRegistry,
    state: PlanExecuteState,
    current_plan: list[dict[str, Any]],
    plan: list[str],
    task: str,
) -> dict[str, Any] | None:
    """Stop risky actions before any tool is executed."""
    if not plan_step:
        return None

    decision = assess_plan_step(plan_step, tool_registry=registry, incident=state.get("incident"))
    if decision.policy == "allow":
        return None

    postponed_update = _postpone_risky_step_until_read_only_evidence_complete(
        plan_step,
        decision,
        current_plan,
        task,
    )
    if postponed_update is not None:
        return postponed_update

    logger.warning(
        f"Risk Controller 拦截步骤 {plan_step.step_id}: "
        f"policy={decision.policy}, risk={decision.risk_level}, reason={decision.reason}"
    )
    trace_service.record_risk_decision(
        trace_id=state.get("trace_id") or "trace-unknown",
        incident_id=_extract_incident_id(state),
        step_id=plan_step.step_id,
        action=decision.action,
        policy=decision.policy,
        risk_level=decision.risk_level,
        reason=decision.reason,
        matched_rules=decision.matched_rules,
        status="blocked",
    )
    response = _generate_risk_stop_response(decision)
    state_update: dict[str, Any] = {
        "current_plan": current_plan,
        "plan": plan,
        "past_steps": [(task, response)],
        "executed_steps": [_mark_step(plan_step, "skipped")],
        "gathered_evidence": [
            _risk_decision_to_evidence(decision, plan_step).model_dump(mode="json")
        ],
        "risk_assessment": decision.to_risk_assessment().model_dump(mode="json"),
        "response": response,
    }

    if decision.policy == "approval_required":
        approval = _create_approval_request(state, plan_step, decision)
        state_update["pending_approval"] = approval.model_dump(mode="json")
        return state_update

    state_update["pending_approval"] = None
    state_update["errors"] = [decision.reason]
    return state_update


def _postpone_risky_step_until_read_only_evidence_complete(
    plan_step: PlanStep,
    decision: RiskControlDecision,
    current_plan: list[dict[str, Any]],
    task: str,
) -> dict[str, Any] | None:
    """Move approval-required actions after remaining low-risk read-only diagnostics."""
    if decision.policy != "approval_required":
        return None
    if len(current_plan) <= 1:
        return None

    remaining_steps: list[PlanStep] = []
    for raw_step in current_plan[1:]:
        parsed = parse_plan_step(raw_step)
        if parsed is None:
            return None
        remaining_steps.append(parsed)

    read_only_steps = [
        step
        for step in remaining_steps
        if step.risk_level == "low" and step.tool_name != "suggest_remediation"
    ]
    if not read_only_steps:
        return None

    reordered_steps = (
        read_only_steps
        + [step for step in remaining_steps if step not in read_only_steps]
        + [plan_step]
    )
    update = normalize_plan_state_update(reordered_steps)
    update["past_steps"] = [
        (
            task,
            "Approval-required action postponed until remaining read-only diagnostics complete.",
        )
    ]
    return update


def _create_approval_request(
    state: PlanExecuteState,
    step: PlanStep,
    decision: RiskControlDecision,
) -> ApprovalRequest:
    """Persist an approval request for a blocked step."""
    normalized_decision = decision
    if not decision.step_id or not decision.tool_name:
        normalized_decision = decision.model_copy(
            update={
                "step_id": decision.step_id or step.step_id,
                "tool_name": decision.tool_name or step.tool_name,
            }
        )
    return create_approval_request_from_risk_decision(
        state,
        normalized_decision,
        approval_repository=approval_service,
    )


def _record_and_dump_tool_call(
    result: ToolExecutionResult,
    step: PlanStep,
    state: PlanExecuteState,
) -> dict[str, Any]:
    """Create a tool-call record, write trace, and return JSON-safe data."""
    record = _tool_result_to_call_record(result, step, state)
    trace_service.record_tool_call(record)
    return record.model_dump(mode="json")


def _generate_risk_stop_response(decision: RiskControlDecision) -> str:
    """Render the stop reason for the user-facing diagnosis stream."""
    if decision.policy == "forbidden":
        title = "AIOps 已拦截危险动作"
        next_step = "该动作不会自动执行，请由人工在变更流程中重新评估。"
    else:
        title = "AIOps 诊断已暂停，等待人工审批"
        next_step = "审批通过前，Agent 不会自动执行该动作。"

    return f"""# {title}

## 动作
{decision.action}

## 风险等级
{decision.risk_level}

## 策略
{decision.policy}

## 原因
{decision.reason}

## 下一步
{next_step}
"""


def _risk_decision_to_evidence(decision: RiskControlDecision, step: PlanStep) -> Evidence:
    """Represent risk and approval decisions as first-class evidence."""
    summary = f"风险策略 {decision.policy} 拦截动作 {decision.action}: {decision.reason}"
    if decision.policy == "approval_required":
        inference = "后续动作需要人工审批，Agent 暂停自动执行。"
        next_step = "在审批中心完成 approve/reject，并由人工按变更流程处理。"
    elif decision.policy == "forbidden":
        inference = "该动作被判定为禁止自动执行，不能作为已执行处置。"
        next_step = "转人工变更流程重新评估，必要时补充只读诊断证据。"
    else:
        inference = "风险策略已记录。"
        next_step = "继续执行低风险只读诊断步骤。"
    return Evidence(
        source_tool="risk_controller",
        step_id=step.step_id,
        summary=summary,
        evidence_type="risk",
        data_source="rule_based",
        stance="supporting",
        confidence_reason="风险策略规则命中",
        fact=f"policy={decision.policy}, risk={decision.risk_level}, action={decision.action}",
        inference=inference,
        uncertainty="风险判断来自规则策略，最终生产变更仍需人工确认。",
        next_step=next_step,
        raw_data={
            "status": "success",
            "output": {
                "source": "rule_based",
                "policy": decision.policy,
                "risk_level": decision.risk_level,
                "action": decision.action,
                "reason": decision.reason,
                "matched_rules": decision.matched_rules,
            },
        },
        confidence=0.7,
        related_hypothesis=step.expected_evidence,
    )


def _get_current_plan_step(current_plan: list[dict[str, Any]]) -> PlanStep | None:
    """Return the next structured PlanStep from state."""
    if not current_plan:
        return None
    step = parse_plan_step(current_plan[0])
    if not step:
        logger.warning("结构化计划步骤解析失败，回退到旧 plan")
    return step


def _format_plan_step_for_execution(step: PlanStep) -> str:
    """Format PlanStep into an execution prompt and past_steps label."""
    return (
        f"[{step.step_id}] 使用 {step.tool_name} 执行: {step.purpose}\n"
        f"输入参数: {json.dumps(step.input_args, ensure_ascii=False, default=str)}\n"
        f"预期证据: {step.expected_evidence}\n"
        f"风险等级: {step.risk_level}"
    )


def _mark_step(step: PlanStep | None, status: str) -> dict[str, Any] | None:
    """Return a JSON-safe executed step snapshot."""
    if not step:
        return None
    return step.model_copy(update={"status": status}).model_dump(mode="json")


def _result_for_persistence(result: ToolExecutionResult) -> ToolExecutionResult:
    """Return a copy with bulky external raw payloads removed for Trace/Report storage."""
    redacted_args = _redact_sensitive_data(result.input_args)
    redacted_output = _redact_sensitive_data(result.output)
    updates: dict[str, Any] = {}
    if redacted_args != result.input_args:
        updates["input_args"] = redacted_args
    if redacted_output != result.output:
        updates["output"] = redacted_output
    persisted_result = result.model_copy(update=updates) if updates else result
    if config.aiops_store_raw_external_payload:
        return persisted_result
    if not isinstance(persisted_result.output, dict):
        return persisted_result
    compact_output = _compact_external_payload(persisted_result.output)
    compact_output = _redact_sensitive_data(compact_output)
    if compact_output is persisted_result.output:
        return persisted_result
    return persisted_result.model_copy(update={"output": compact_output})


def _compact_external_payload(output: dict[str, Any]) -> dict[str, Any]:
    source = str(output.get("source") or "")
    if source not in {
        "prometheus",
        "loki",
        "log_gateway",
        "cmdb",
        "deploy_history",
        "redis_info",
        "kubernetes",
        "mysql",
        "ticket_api",
        "alertmanager",
        "jaeger",
        "tempo",
        "redpanda",
    }:
        return output
    compact = dict(output)
    raw = compact.get("raw")
    if isinstance(raw, dict):
        compact["raw"] = _compact_raw_payload(raw)
        compact["raw_truncated"] = True
    return compact


def _compact_raw_payload(raw: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            compact[key] = {
                nested_key: nested_value
                for nested_key, nested_value in value.items()
                if nested_key
                in {
                    "connected_clients",
                    "blocked_clients",
                    "maxclients",
                    "used_memory",
                    "maxmemory",
                    "Threads_connected",
                    "Max_used_connections",
                    "Slow_queries",
                    "Innodb_row_lock_waits",
                }
                or str(nested_key).startswith("db")
            }
            if len(compact[key]) != len(value):
                compact[key]["_raw_truncated"] = True
        elif isinstance(value, list):
            compact[key] = value[:5]
        else:
            compact[key] = value
    return compact


def _tool_result_to_evidence(result: ToolExecutionResult, step: PlanStep) -> Evidence:
    """Convert a tool result into audit-ready diagnostic evidence."""
    raw_data = result.model_dump(mode="json")
    stance = infer_evidence_stance(
        source_tool=result.tool_name,
        raw_data=raw_data,
        summary=_summarize_tool_result(result),
    )
    data_source = normalize_data_source(result.tool_name, raw_data)
    confidence = _evidence_confidence(result, data_source)
    execution_path = result.metadata.get("execution_path")
    if execution_path == "manual_analysis":
        confidence = min(confidence, 0.35)
    elif execution_path == "llm_toolnode_fallback":
        confidence = 0.1 if result.status == "failed" else min(confidence, 0.35)

    return Evidence(
        source_tool=result.tool_name,
        step_id=step.step_id,
        summary=_summarize_tool_result(result),
        evidence_type=infer_evidence_type(result.tool_name),
        data_source=data_source,
        stance=stance,
        confidence_reason=build_confidence_reason(
            source_tool=result.tool_name,
            raw_data=raw_data,
            stance=stance,
        ),
        fact=_build_evidence_fact(result, data_source),
        inference=_build_evidence_inference(result, stance),
        uncertainty=_build_evidence_uncertainty(result, data_source),
        next_step=_build_evidence_next_step(result, data_source, step),
        raw_data=raw_data,
        confidence=confidence,
        related_hypothesis=step.expected_evidence,
    )


def _tool_result_to_call_record(
    result: ToolExecutionResult,
    step: PlanStep,
    state: PlanExecuteState,
) -> ToolCallRecord:
    """Convert a tool result into a replayable tool call audit record."""
    return ToolCallRecord(
        trace_id=state.get("trace_id") or "trace-unknown",
        incident_id=_extract_incident_id(state),
        step_id=step.step_id,
        tool_name=result.tool_name,
        input_args=result.input_args,
        input_summary=_summarize_input_args(result.input_args),
        output=result.output,
        output_summary=_summarize_tool_result(result),
        data_source=normalize_data_source(result.tool_name, result.model_dump(mode="json")),
        latency_ms=result.latency_ms,
        status=result.status,
        risk_level=result.risk_level,
        read_only=result.read_only,
        error_message=result.error_message,
    )


def _extract_incident_id(state: PlanExecuteState) -> str:
    """Return incident_id from dict or model-like state values."""
    incident = state.get("incident") or {}
    if isinstance(incident, dict):
        return str(incident.get("incident_id") or "incident-unknown")
    return str(getattr(incident, "incident_id", "incident-unknown"))


def _summarize_tool_result(result: ToolExecutionResult) -> str:
    """Create a compact human-readable evidence summary."""
    if result.status == "failed":
        return f"工具 {result.tool_name} 调用失败: {result.error_message or '未知错误'}"

    output = result.output
    if isinstance(output, dict):
        summary = output.get("summary")
        if summary:
            return str(summary)
        return f"工具 {result.tool_name} 返回 {len(output)} 个结构化字段"
    if isinstance(output, list):
        return f"工具 {result.tool_name} 返回 {len(output)} 条记录"
    if output is None:
        return f"工具 {result.tool_name} 调用成功，但未返回输出"

    text = str(output).strip()
    return text[:300] if len(text) > 300 else text


def _summarize_input_args(input_args: dict[str, Any]) -> str:
    """Create a compact, non-secret input summary for tool audit displays."""
    if not input_args:
        return "无输入参数"
    safe_args = _redact_sensitive_data(input_args)
    text = json.dumps(safe_args, ensure_ascii=False, default=str)
    return text[:220] + "..." if len(text) > 220 else text


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(
        token in lowered
        for token in [
            "password",
            "passwd",
            "pwd",
            "token",
            "secret",
            "key",
            "dsn",
            "authorization",
            "cookie",
            "credential",
            "bearer",
        ]
    )


def _redact_sensitive_data(value: Any) -> Any:
    """Recursively redact sensitive values before durable audit persistence."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_key(str(key)) else _redact_sensitive_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_data(item) for item in value]
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|"
    r"authorization|cookie|credential|dsn)\b\s*([=:])\s*(?!Bearer\b)([^,\s;&]+)"
)


def _redact_sensitive_text(text: str) -> str:
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    return _SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        redacted,
    )


def _evidence_confidence(result: ToolExecutionResult, data_source: str) -> float:
    """Score confidence from status and provenance, not just successful execution."""
    if result.status == "failed":
        return 0.05 if data_source == "not_configured" else 0.1
    if data_source in {
        "prometheus",
        "loki",
        "log_gateway",
        "cmdb",
        "deploy_history",
        "redis_info",
        "kubernetes",
        "mysql",
        "ticket_api",
        "alertmanager",
        "jaeger",
        "tempo",
        "redpanda",
    }:
        return 0.82
    if data_source in {"mcp_monitor", "mcp_cls", "rag"}:
        return 0.72
    if data_source == "mcp_monitor_mixed":
        return 0.6
    if data_source == "mock":
        return 0.5
    if data_source == "rule_based":
        return 0.65
    if data_source == "failed":
        return 0.1
    return 0.65


def _build_evidence_fact(result: ToolExecutionResult, data_source: str) -> str:
    """Separate directly observed data from later diagnostic inference."""
    if result.status == "failed":
        return f"{result.tool_name} 未返回可用数据，来源={data_source}"
    summary = _summarize_tool_result(result)
    return f"{summary}；来源={data_source}"


def _build_evidence_inference(result: ToolExecutionResult, stance: str) -> str:
    """Summarize what this evidence does to the active hypothesis."""
    if result.status == "failed":
        return "该步骤不能支持根因判断，只能作为证据缺口记录。"
    if stance == "supporting":
        return "该证据支持当前根因假设。"
    if stance == "refuting":
        return "该证据与当前根因假设不一致，需要补充其他证据。"
    if stance == "unknown":
        return "该证据当前无法判断立场，只能作为证据缺口或待复核记录。"
    return "该证据目前只提供背景信息，尚不足以单独确认根因。"


def _build_evidence_uncertainty(result: ToolExecutionResult, data_source: str) -> str:
    """Make mock, fallback, and failure boundaries explicit."""
    if data_source == "not_configured":
        return "真实适配器未配置且 Mock 回退关闭，不能生成真实系统证据。"
    if data_source == "failed":
        return result.error_message or "真实适配器调用失败，证据不完整。"
    if data_source == "mock":
        return "该证据来自 Mock 回退，只适合本地演示，不代表真实生产状态。"
    if data_source in {"rule_based", "manual_analysis", "llm_toolnode_fallback"}:
        return "该结果来自规则或人工/LLM 兜底路径，需要结合真实工具证据复核。"
    if result.status == "failed":
        return result.error_message or "工具调用失败，证据不完整。"
    return ""


def _build_evidence_next_step(
    result: ToolExecutionResult,
    data_source: str,
    step: PlanStep,
) -> str:
    """Recommend the next verification action based on provenance and status."""
    if data_source == "not_configured":
        return f"配置 {result.tool_name} 对应真实适配器，或开启 Mock 模式后仅作演示。"
    if result.status == "failed":
        return "检查工具配置、网络、权限或超时设置后重试。"
    if data_source == "mock":
        return "接入真实适配器后重复该步骤，确认 Mock 结论是否成立。"
    if step.expected_evidence:
        return f"用后续步骤交叉验证：{step.expected_evidence}"
    return "继续执行计划中的后续证据采集步骤。"


def _format_tool_error(tool_call_record: dict[str, Any]) -> str:
    """Render failed tool call as a state error string."""
    return (
        f"工具 {tool_call_record.get('tool_name')} "
        f"步骤 {tool_call_record.get('step_id')} 调用失败: "
        f"{tool_call_record.get('error_message') or '未知错误'}"
    )
