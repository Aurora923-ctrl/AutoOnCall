"""
Executor 节点：执行单个步骤
基于 LangGraph 官方教程实现
"""

import asyncio
import json
from typing import Any

from loguru import logger

from app.agent.mcp_client import get_mcp_client_with_retry
from app.models.approval import ApprovalRequest
from app.models.evidence import Evidence
from app.models.plan import PlanStep
from app.models.trace import ToolCallRecord
from app.services.aiops_execution_records import (
    format_tool_error,
    result_for_persistence,
    summarize_input_args,
    summarize_tool_result,
    tool_result_to_call_record,
    tool_result_to_evidence,
)
from app.services.aiops_state_utils import extract_incident_id
from app.services.approval_service import approval_service
from app.services.approval_workflow import (
    create_approval_request_from_risk_decision,
    generate_risk_stop_response,
)
from app.services.trace_service import trace_service
from app.tools import get_current_time, retrieve_knowledge
from app.tools.base import ToolExecutionResult
from app.tools.registry import ToolRegistry, create_default_tool_registry
from app.utils.log_safety import summarize_text_for_log
from app.utils.public_errors import public_exception_message

from .execution_fallbacks import (
    ensure_plan_step,
    execute_with_llm_tools,
    fallback_text_to_tool_result,
    fallback_warnings,
    message_content_to_text,
    safe_fallback_tools,
)
from .risk_controller import RiskControlDecision, assess_plan_step
from .state import (
    PlanExecuteState,
    normalize_plan_state_update,
    parse_plan_step,
    remaining_plan_state_update,
)

READ_ONLY_EVIDENCE_FANOUT_LIMIT = 4
READ_ONLY_EVIDENCE_FANOUT_TOOLS = {
    "query_metrics",
    "query_logs",
    "query_redis_status",
    "query_mysql_status",
    "query_k8s_status",
    "search_runbook",
    "search_history_ticket",
    "query_service_context",
    "query_deploy_history",
}


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
    logger.info(f"当前任务: {summarize_text_for_log(task, label='task')}")

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

        registry = create_default_tool_registry(all_tools).with_incident_context(
            state.get("incident")
        )
        logger.info(f"Tool Registry 已加载 {len(registry.list_tools())} 个标准工具")

        risk_block_update = _risk_gate_state_update(
            plan_step, registry, state, current_plan, plan, task
        )
        if risk_block_update is not None:
            return risk_block_update

        gathered_evidence: list[dict[str, Any]] = []
        tool_call_records: list[dict[str, Any]] = []
        past_steps: list[tuple[str, str]] = []
        executed_steps: list[dict[str, Any]] = []
        errors: list[str] = []
        warnings: list[str] = []
        consumed_count = 1

        fanout_steps = _select_read_only_evidence_fanout_steps(
            plan_step,
            current_plan,
            registry,
            state,
        )
        if len(fanout_steps) > 1:
            batch_results = await _execute_registered_step_fanout(fanout_steps, registry, state)
            consumed_count = len(fanout_steps)
            for batch_step, (result, step_status, evidence, tool_call_record) in zip(
                fanout_steps,
                batch_results,
                strict=False,
            ):
                gathered_evidence.append(evidence)
                tool_call_records.append(tool_call_record)
                past_steps.append((_format_plan_step_for_execution(batch_step), result))
                marked = _mark_step(batch_step, step_status)
                if marked:
                    executed_steps.append(marked)
                if step_status == "failed":
                    errors.append(_format_tool_error(tool_call_record))
        else:
            direct_result = await _try_execute_registered_step(plan_step, registry, state)
            if direct_result is not None:
                result, step_status, evidence, tool_call_record = direct_result
                gathered_evidence.append(evidence)
                tool_call_records.append(tool_call_record)
                past_steps.append((task, result))
                marked = _mark_step(plan_step, step_status) if plan_step else None
                if marked:
                    executed_steps.append(marked)
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
                past_steps.append((task, result))
                marked = _mark_step(plan_step, step_status) if plan_step else None
                if marked:
                    executed_steps.append(marked)
                warnings.extend(_fallback_warnings(tool_call_record, plan_step))
                if step_status == "failed":
                    errors.append(_format_tool_error(tool_call_record))

        logger.info(
            f"Executor 完成 {len(past_steps)} 个步骤，"
            f"结果总长度: {sum(len(result) for _, result in past_steps)}"
        )

        state_update = {
            **remaining_plan_state_update(current_plan, plan, consumed=consumed_count),
            "past_steps": past_steps,  # 使用 operator.add 追加
            "executed_steps": executed_steps,
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
        public_message = public_exception_message(e, fallback="步骤执行失败，请检查服务端日志")
        logger.error(
            "执行步骤失败: "
            f"error_type={type(e).__name__}, {summarize_text_for_log(e, label='error')}"
        )
        executed_step = _mark_step(plan_step, "failed") if plan_step else None
        state_update = {
            **remaining_plan_state_update(current_plan, plan),
            "past_steps": [(task, public_message)],
            "executed_steps": [executed_step] if executed_step else [],
            "errors": [public_message],
        }
        if plan_step:
            failed_result = ToolExecutionResult(
                tool_name=plan_step.tool_name,
                status="failed",
                input_args=plan_step.input_args,
                error_message=public_message,
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
    """Compatibility wrapper for the extracted fallback executor."""
    return await execute_with_llm_tools(task, all_tools)


def _message_content_to_text(content: Any) -> str:
    """Compatibility wrapper for fallback message rendering."""
    return message_content_to_text(content)


def _safe_fallback_tools(all_tools: list[Any]) -> list[Any]:
    """Compatibility wrapper for fallback tool filtering."""
    return safe_fallback_tools(all_tools)


async def _try_execute_registered_step(
    plan_step: PlanStep | None,
    registry: ToolRegistry,
    state: PlanExecuteState,
    *,
    batch_metadata: dict[str, Any] | None = None,
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
    result = await registry.arun(
        plan_step.tool_name,
        plan_step.input_args,
        incident=state.get("incident"),
        step=plan_step,
    )
    if batch_metadata:
        metadata = dict(result.metadata or {})
        metadata["evidence_batch"] = dict(batch_metadata)
        result = result.model_copy(update={"metadata": metadata})
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


def _select_read_only_evidence_fanout_steps(
    plan_step: PlanStep | None,
    current_plan: list[dict[str, Any]],
    registry: ToolRegistry,
    state: PlanExecuteState,
) -> list[PlanStep]:
    """Return adjacent low-risk read-only evidence steps that can run together."""
    if not plan_step or len(current_plan) <= 1:
        return []

    selected: list[PlanStep] = []
    for raw_step in current_plan:
        parsed_step = parse_plan_step(raw_step)
        if parsed_step is None:
            break
        if not _is_read_only_evidence_fanout_step(parsed_step, registry, state):
            break
        selected.append(parsed_step)
        if len(selected) >= READ_ONLY_EVIDENCE_FANOUT_LIMIT:
            break
    return selected


def _is_read_only_evidence_fanout_step(
    step: PlanStep,
    registry: ToolRegistry,
    state: PlanExecuteState,
) -> bool:
    """Keep fan-out limited to deterministic low-risk evidence collection tools."""
    if step.risk_level != "low":
        return False
    if step.tool_name not in READ_ONLY_EVIDENCE_FANOUT_TOOLS:
        return False
    tool = registry.get(step.tool_name)
    if not tool or not getattr(tool, "read_only", False):
        return False
    decision = assess_plan_step(step, tool_registry=registry, incident=state.get("incident"))
    return decision.policy == "allow" and decision.read_only and decision.risk_level == "low"


async def _execute_registered_step_fanout(
    steps: list[PlanStep],
    registry: ToolRegistry,
    state: PlanExecuteState,
) -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    """Execute a bounded batch of read-only steps while preserving plan order."""
    batch_id = f"fanout-{steps[0].step_id}-{len(steps)}"
    tasks = [
        _execute_registered_fanout_item(
            step,
            registry,
            state,
            batch_metadata={
                "batch_id": batch_id,
                "batch_size": len(steps),
                "batch_index": index,
                "execution_mode": "bounded_read_only_fanout",
            },
        )
        for index, step in enumerate(steps, 1)
    ]
    return await asyncio.gather(*tasks)


async def _execute_registered_fanout_item(
    step: PlanStep,
    registry: ToolRegistry,
    state: PlanExecuteState,
    *,
    batch_metadata: dict[str, Any],
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Execute one fan-out item; a failure becomes failed evidence, not batch failure."""
    try:
        result = await _try_execute_registered_step(
            step,
            registry,
            state,
            batch_metadata=batch_metadata,
        )
        if result is not None:
            return result
    except Exception as exc:
        logger.warning(
            f"并行只读取证步骤 {step.step_id}/{step.tool_name} 失败: "
            f"error_type={type(exc).__name__}, {summarize_text_for_log(exc, label='error')}"
        )

    public_message = "并行只读取证步骤执行失败，请检查服务端日志"
    failed_result = ToolExecutionResult(
        tool_name=step.tool_name,
        status="failed",
        input_args=step.input_args,
        error_message=public_message,
        metadata={"evidence_batch": dict(batch_metadata)},
    )
    persisted_result = _result_for_persistence(failed_result)
    evidence = _tool_result_to_evidence(persisted_result, step)
    tool_call_record = _record_and_dump_tool_call(persisted_result, step, state)
    return (
        json.dumps(
            persisted_result.model_dump(mode="json"),
            ensure_ascii=False,
            default=str,
            indent=2,
        ),
        "failed",
        evidence.model_dump(mode="json"),
        tool_call_record,
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
    """Compatibility wrapper for fallback step normalization."""
    return ensure_plan_step(plan_step, task)


def _fallback_text_to_tool_result(
    task: str,
    result_text: str,
    plan_step: PlanStep,
) -> ToolExecutionResult:
    """Compatibility wrapper for fallback result normalization."""
    return fallback_text_to_tool_result(task, result_text, plan_step)


def _fallback_warnings(
    tool_call_record: dict[str, Any],
    plan_step: PlanStep | None,
) -> list[str]:
    """Compatibility wrapper for fallback warnings."""
    return fallback_warnings(tool_call_record, plan_step)


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
        incident_id=extract_incident_id(state),
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
    """Compatibility wrapper for the shared risk-stop renderer."""
    return generate_risk_stop_response(decision)


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
    """Compatibility wrapper for the extracted execution-record builder."""
    return result_for_persistence(result)


def _tool_result_to_evidence(result: ToolExecutionResult, step: PlanStep) -> Evidence:
    """Compatibility wrapper for tests and older imports."""
    return tool_result_to_evidence(result, step)


def _tool_result_to_call_record(
    result: ToolExecutionResult,
    step: PlanStep,
    state: PlanExecuteState,
) -> ToolCallRecord:
    """Compatibility wrapper for tests and older imports."""
    return tool_result_to_call_record(result, step, state)


def _summarize_tool_result(result: ToolExecutionResult) -> str:
    """Compatibility wrapper for tests and older imports."""
    return summarize_tool_result(result)


def _summarize_input_args(input_args: dict[str, Any]) -> str:
    """Compatibility wrapper for tests and older imports."""
    return summarize_input_args(input_args)


def _format_tool_error(tool_call_record: dict[str, Any]) -> str:
    """Compatibility wrapper for tests and older imports."""
    return format_tool_error(tool_call_record)
