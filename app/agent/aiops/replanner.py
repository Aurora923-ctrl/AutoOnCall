"""
Replanner 节点：重新规划或生成最终响应
基于 LangGraph 官方教程实现
"""

from textwrap import dedent
from typing import Any, Literal, cast

from langchain_core.prompts import ChatPromptTemplate
from langchain_qwq import ChatQwen
from loguru import logger
from pydantic import BaseModel, Field

from app.config import config
from app.models.plan import PlanStep
from app.services.aiops_state_utils import extract_incident_id
from app.services.approval_service import approval_service
from app.services.approval_workflow import (
    build_change_plan_from_risk_decision,
    create_approval_request_from_risk_decision,
    generate_approval_waiting_response,
    generate_forbidden_response,
)
from app.services.context_budget import DEFAULT_CONTEXT_BUDGETER, ContextBudgeter
from app.services.incident_lifecycle import infer_terminal_report_status
from app.services.report_generator import report_generator
from app.services.trace_service import trace_service
from app.tools.registry import create_default_tool_registry

from .evidence_analyzer import EvidenceAnalysis, analyze_evidence, render_analysis_summary
from .risk_controller import RiskControlDecision, assess_plan_step
from .state import PlanExecuteState, normalize_plan_state_update
from .utils import format_tools_description

MAX_STEPS = 8
LLM_DECISION_SAFE_SKIP_DECISIONS = {"retry_failed_tool", "request_approval"}
REPLANNER_CONTEXT_CHAR_LIMIT = 3000


class Response(BaseModel):
    """最终响应的格式"""

    response: str = Field(description="对用户的最终响应")


class ReplanDecision(BaseModel):
    """Structured Replanner decision."""

    decision: Literal[
        "continue_investigation",
        "add_steps",
        "retry_failed_tool",
        "request_approval",
        "generate_report",
        "escalate_to_human",
    ] = Field(
        description="""下一步行动：
        - continue_investigation: 当前剩余计划合理，继续执行
        - add_steps: 追加缺失证据采集步骤
        - retry_failed_tool: 重试失败工具或替代工具
        - request_approval: 修复动作需要人工审批
        - generate_report: 证据足够，生成报告
        - escalate_to_human: 证据不足且无法安全自动继续"""
    )
    reason: str = Field(default="", description="决策原因")
    new_steps: list[PlanStep] = Field(
        default_factory=list, description="需要追加或重试的结构化步骤"
    )


replanner_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            dedent("""
                作为一个重新规划专家，你需要根据已执行的步骤决定下一步行动。

                可用工具列表（用于制定计划时参考）：

                {tools_description}

                注意：你的职责是制定或调整计划，实际的工具调用由 Executor 负责执行。

                你需要输出结构化决策：

                - generate_report: 证据足够，立即生成最终响应
                - continue_investigation: 当前计划合理，继续执行下一个步骤
                - add_steps: 当前计划缺少关键证据，追加只读排查步骤
                - retry_failed_tool: 工具调用失败，重试一次或选择替代只读工具
                - request_approval: 后续动作会影响线上系统，需要人工审批
                - escalate_to_human: 证据不足且无法安全自动继续，需要人工介入

                评估标准：
                - 当前信息是否已经足够解决用户问题？【最关键】
                - gathered_evidence 是否已经覆盖指标、日志、依赖状态等关键证据？
                - 是否存在 failed tool，需要重试或替代？
                - 剩余步骤是否真的"必需"？
                - 已执行步骤数是否过多（>= 5）？如果是，优先 generate_report 或 escalate_to_human
                - 新增步骤只能使用可用工具列表中的只读诊断工具；不得输出会修改生产系统的动作
                - 如果需要变更、重启、扩容、回滚、执行 SQL 或修改配置，只能选择 request_approval
                - 不得绕过 Evidence Analyzer 的失败重试、风险控制和审批门禁

                **决策优先级口诀：**
                "证据足够就报告，证据缺失才补查，失败工具只重试一次"
                "信息足够就响应，不要追求完美"
            """).strip(),
        ),
        ("placeholder", "{messages}"),
    ]
)

response_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            dedent("""
                根据原始任务和已执行步骤的结果，生成一个全面的最终响应。

                响应要求：
                - 清晰、结构化
                - 基于实际数据，不要编造
                - 如果某些步骤失败，要诚实说明
                - 使用 Markdown 格式
            """).strip(),
        ),
        ("placeholder", "{messages}"),
    ]
)


async def replanner(state: PlanExecuteState) -> dict[str, Any]:
    """
    重新规划节点：决定是继续、调整计划还是生成最终响应

    决策类型：
    - continue_investigation: 继续执行当前计划
    - add_steps: 追加缺失证据采集步骤
    - retry_failed_tool: 重试失败工具
    - generate_report: 生成最终响应
    - escalate_to_human: 升级人工
    """
    logger.info("=== Replanner：重新规划 ===")

    if state.get("response"):
        logger.info("状态中已存在最终响应或风险拦截响应，保持当前结果")
        update = {
            "response": state.get("response"),
            "pending_approval": state.get("pending_approval"),
            "risk_assessment": state.get("risk_assessment"),
        }
        return _with_generated_report(state, update, status=_infer_report_status(update))

    if state.get("pending_approval"):
        logger.info("检测到待审批动作，保持暂停状态")
        update = {
            "pending_approval": state.get("pending_approval"),
            "risk_assessment": state.get("risk_assessment"),
            "response": state.get("response") or _generate_approval_waiting_response(dict(state)),
        }
        return _with_generated_report(state, update, status="waiting_approval")

    plan = state.get("plan", [])
    past_steps = state.get("past_steps", [])
    analysis = analyze_evidence(state)
    analysis_decision = _decision_from_analysis(analysis)
    state_update = _analysis_state_update(analysis)

    if len(past_steps) >= MAX_STEPS:
        _record_replanner_decision(
            state,
            analysis_decision,
            analysis,
            decision_source="max_steps_guard",
        )
        logger.warning(
            f"已执行 {len(past_steps)} 个步骤，超过最大限制 {MAX_STEPS}，强制生成最终响应"
        )
        return await _generate_response_with_analysis(state, analysis)

    decision, decision_source = await _decide_with_llm_or_analysis(
        state,
        analysis,
        analysis_decision,
    )
    _record_replanner_decision(state, decision, analysis, decision_source=decision_source)
    logger.info(f"剩余计划步骤: {len(plan)}")
    logger.info(f"已执行步骤: {len(past_steps)}")
    logger.info(f"Evidence Analyzer 决策: {analysis.decision} - {analysis.reason}")
    logger.info(
        f"Replanner 结构化决策: {decision.decision} - {decision.reason} (source={decision_source})"
    )

    if decision.decision == "generate_report":
        risk_gate_update = _approval_state_update(state, decision.reason, force=False)
        if risk_gate_update:
            logger.warning("证据已足够，但剩余计划包含风险动作，先暂停进入风险控制流程")
            state_update.update(risk_gate_update)
            return _with_generated_report(
                state,
                state_update,
                status=_infer_report_status(state_update),
            )

        logger.info("证据充足，生成最终响应")
        response_update = await _generate_response_with_analysis(state, analysis)
        response_update.update(state_update)
        return response_update

    if decision.decision == "retry_failed_tool" and decision.new_steps:
        logger.info(f"重试失败工具: {decision.new_steps[0].tool_name}")
        return _merge_updates(state_update, _steps_to_state_update(decision.new_steps))

    if decision.decision == "add_steps" and decision.new_steps:
        logger.info(f"追加证据采集步骤: {len(decision.new_steps)}")
        return _merge_updates(state_update, _steps_to_state_update(decision.new_steps))

    if decision.decision == "escalate_to_human":
        logger.warning("证据不足且无法安全继续，升级人工处理")
        state_update["response"] = _generate_escalation_response(state, analysis)
        state_update["errors"] = [analysis.reason]
        return _with_generated_report(state, state_update, status="escalated")

    if decision.decision == "request_approval":
        logger.warning("后续动作需要人工审批，暂停自动执行")
        state_update.update(_approval_state_update(state, decision.reason, force=True))
        return _with_generated_report(
            state,
            state_update,
            status=_infer_report_status(state_update),
        )

    risk_gate_update = _approval_state_update(state, decision.reason, force=False)
    if risk_gate_update:
        logger.warning("剩余计划包含需要审批或禁止自动执行的动作，暂停自动执行")
        state_update.update(risk_gate_update)
        return _with_generated_report(
            state,
            state_update,
            status=_infer_report_status(state_update),
        )

    logger.info("继续执行当前剩余计划")
    return state_update


def _decision_from_analysis(analysis: EvidenceAnalysis) -> ReplanDecision:
    """Normalize EvidenceAnalysis into the Replanner decision contract."""
    new_steps: list[PlanStep] = []
    if analysis.decision == "retry_failed_tool":
        new_steps = analysis.retry_steps
    elif analysis.decision == "add_steps":
        new_steps = analysis.recommended_steps

    return ReplanDecision(
        decision=analysis.decision,
        reason=analysis.reason,
        new_steps=new_steps,
    )


async def _decide_with_llm_or_analysis(
    state: PlanExecuteState,
    analysis: EvidenceAnalysis,
    analysis_decision: ReplanDecision,
) -> tuple[ReplanDecision, str]:
    """Let an optional structured LLM critic refine the deterministic evidence decision."""
    if not config.aiops_replanner_llm_enabled:
        return analysis_decision, "evidence_analyzer"

    if analysis_decision.decision in LLM_DECISION_SAFE_SKIP_DECISIONS:
        return analysis_decision, "evidence_analyzer_safety_priority"

    try:
        llm_decision = await _generate_llm_replan_decision(
            state,
            analysis,
            analysis_decision,
            _create_llm(),
        )
        normalized_decision = _normalize_llm_replan_decision(
            llm_decision,
            state,
            analysis,
            analysis_decision,
        )
        if normalized_decision is not None:
            return normalized_decision, "llm_structured"
    except Exception as exc:
        logger.warning(f"Replanner LLM 决策不可用，回退到 Evidence Analyzer: {exc}")

    return analysis_decision, "evidence_analyzer_fallback"


async def _generate_llm_replan_decision(
    state: PlanExecuteState,
    analysis: EvidenceAnalysis,
    analysis_decision: ReplanDecision,
    llm: Any,
) -> Any:
    """Invoke the structured Replanner prompt with compact runtime context."""
    replan_chain = replanner_prompt | llm.with_structured_output(ReplanDecision)
    return await replan_chain.ainvoke(
        {
            "tools_description": _format_replanner_tools_description(),
            "messages": _build_replanner_messages(state, analysis, analysis_decision),
        }
    )


def _normalize_llm_replan_decision(
    decision_obj: Any,
    state: PlanExecuteState,
    analysis: EvidenceAnalysis,
    analysis_decision: ReplanDecision,
) -> ReplanDecision | None:
    """Validate LLM output against deterministic evidence and risk gates."""
    decision = _coerce_replan_decision(decision_obj)
    if decision is None:
        logger.warning("Replanner LLM 返回无法解析的结构化决策，已忽略")
        return None

    reason = (decision.reason or "").strip() or analysis_decision.reason

    if decision.decision == "generate_report" and not analysis.evidence_sufficient:
        logger.warning("Replanner LLM 试图在证据不足时生成报告，已回退到 Evidence Analyzer")
        return None

    if decision.decision == "continue_investigation" and not _has_remaining_plan(state):
        logger.warning("Replanner LLM 选择继续执行但没有剩余计划，已回退到 Evidence Analyzer")
        return None

    if decision.decision == "retry_failed_tool" and not _has_failed_tool_record(state):
        logger.warning("Replanner LLM 选择重试但没有可重试失败工具，已回退到 Evidence Analyzer")
        return None

    if decision.decision in {"add_steps", "retry_failed_tool"}:
        steps = _coerce_plan_steps(decision.new_steps)
        if not steps:
            logger.warning("Replanner LLM 决策需要新步骤但未提供有效 PlanStep，已回退")
            return None
        safe_steps = _safe_llm_steps_or_none(
            steps,
            state,
            retry=decision.decision == "retry_failed_tool",
        )
        if safe_steps is None:
            return None
        return ReplanDecision(decision=decision.decision, reason=reason, new_steps=safe_steps)

    return ReplanDecision(decision=decision.decision, reason=reason, new_steps=[])


def _coerce_replan_decision(decision_obj: Any) -> ReplanDecision | None:
    if isinstance(decision_obj, ReplanDecision):
        return decision_obj
    if isinstance(decision_obj, dict):
        try:
            return ReplanDecision(**decision_obj)
        except Exception:
            return None
    return None


def _coerce_plan_steps(raw_steps: list[Any]) -> list[PlanStep]:
    steps: list[PlanStep] = []
    for raw_step in raw_steps:
        try:
            step = raw_step if isinstance(raw_step, PlanStep) else PlanStep(**raw_step)
        except Exception:
            continue
        steps.append(step.model_copy(update={"status": "pending"}))
    return steps


def _safe_llm_steps_or_none(
    steps: list[PlanStep],
    state: PlanExecuteState,
    *,
    retry: bool,
) -> list[PlanStep] | None:
    """Accept only registered/read-only steps that risk control allows automatically."""
    registry = create_default_tool_registry([])
    safe_steps: list[PlanStep] = []
    for step in steps:
        if step.tool_name != "manual_analysis" and registry.get(step.tool_name) is None:
            logger.warning(f"Replanner LLM 返回未注册工具 {step.tool_name}，已回退")
            return None

        normalized_step = step.model_copy(
            update={
                "status": "pending",
                "retry_count": max(step.retry_count, 1) if retry else 0,
            }
        )
        risk_decision = assess_plan_step(
            normalized_step,
            tool_registry=registry,
            incident=state.get("incident"),
        )
        if risk_decision.policy != "allow":
            logger.warning(
                "Replanner LLM 返回风险步骤，已回退: "
                f"{normalized_step.tool_name} policy={risk_decision.policy}"
            )
            return None
        safe_steps.append(normalized_step)
    return safe_steps


def _has_remaining_plan(state: PlanExecuteState) -> bool:
    return bool(state.get("current_plan") or state.get("plan"))


def _has_failed_tool_record(state: PlanExecuteState) -> bool:
    for record in state.get("tool_call_records", []):
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "").lower()
        step_id = str(record.get("step_id") or "")
        if status in {"failed", "error"} and not step_id.endswith("-retry"):
            return True
    return False


def _format_replanner_tools_description() -> str:
    registry = create_default_tool_registry([])
    return format_tools_description(registry.list_contracts())


def _build_replanner_messages(
    state: PlanExecuteState,
    analysis: EvidenceAnalysis,
    analysis_decision: ReplanDecision,
    budgeter: ContextBudgeter | None = None,
) -> list[tuple[str, str]]:
    active_budgeter = budgeter or DEFAULT_CONTEXT_BUDGETER
    return [
        ("user", f"原始任务: {state.get('input', '')}"),
        (
            "user",
            f"Incident:\n{_json_preview(state.get('incident') or {}, budgeter=active_budgeter)}",
        ),
        (
            "user",
            "剩余结构化计划:\n"
            f"{_json_preview(state.get('current_plan') or [], budgeter=active_budgeter)}",
        ),
        (
            "user",
            f"兼容计划队列:\n{_json_preview(state.get('plan') or [], budgeter=active_budgeter)}",
        ),
        (
            "user",
            "执行历史:\n"
            f"{_text_preview(_format_simple_steps(state.get('past_steps', [])), budgeter=active_budgeter)}",
        ),
        (
            "user",
            "结构化证据:\n"
            f"{_text_preview(_format_evidence_for_prompt(state.get('gathered_evidence', [])), budgeter=active_budgeter)}",
        ),
        (
            "user",
            "工具调用记录:\n"
            f"{_text_preview(_format_tool_calls_for_prompt(state.get('tool_call_records', [])), budgeter=active_budgeter)}",
        ),
        ("user", f"Evidence Analyzer 摘要:\n{render_analysis_summary(analysis)}"),
        (
            "user",
            "Evidence Analyzer 基线决策:\n"
            f"{_json_preview(analysis_decision.model_dump(mode='json'), budgeter=active_budgeter)}",
        ),
        (
            "user",
            "请输出 ReplanDecision。若补查，只给低风险只读步骤；若涉及变更，只能请求审批。",
        ),
    ]


def _json_preview(
    value: Any,
    limit: int = REPLANNER_CONTEXT_CHAR_LIMIT,
    budgeter: ContextBudgeter | None = None,
) -> str:
    active_budgeter = budgeter or DEFAULT_CONTEXT_BUDGETER
    return active_budgeter.json(value, limit=limit)


def _text_preview(
    text: str,
    limit: int = REPLANNER_CONTEXT_CHAR_LIMIT,
    budgeter: ContextBudgeter | None = None,
) -> str:
    active_budgeter = budgeter or DEFAULT_CONTEXT_BUDGETER
    return active_budgeter.text(text, limit=limit)


def _analysis_state_update(analysis: EvidenceAnalysis) -> dict[str, Any]:
    """Return state fields derived from EvidenceAnalysis."""
    update: dict[str, Any] = {
        "hypotheses": analysis.hypotheses,
        "evidence_analysis": analysis.model_dump(mode="json"),
    }
    if analysis.hypotheses:
        update["final_diagnosis"] = analysis.hypotheses[0]
    return update


def _steps_to_state_update(steps: list[PlanStep]) -> dict[str, Any]:
    """Synchronize structured and legacy plan queues."""
    return normalize_plan_state_update(steps)


def _merge_updates(*updates: dict[str, Any]) -> dict[str, Any]:
    """Merge small state update dictionaries."""
    merged: dict[str, Any] = {}
    for update in updates:
        merged.update(update)
    return merged


def _approval_state_update(
    state: PlanExecuteState,
    reason: str,
    force: bool = False,
) -> dict[str, Any]:
    """Build structured risk and approval state for a paused action."""
    risk_decision = _extract_risk_decision(state)
    if risk_decision is None:
        if not force:
            return {}
        normalized_reason = reason or "后续动作可能影响线上系统，需要人工审批后再继续"
        risk_decision = RiskControlDecision(
            action="需要人工确认的后续处置动作",
            risk_level="medium",
            policy="approval_required",
            need_approval=True,
            allowed=False,
            reason=normalized_reason,
            matched_rules=["replanner:forced-approval"],
        )
    elif reason and reason not in risk_decision.reason:
        risk_decision.reason = f"{risk_decision.reason}；{reason}"

    risk_assessment = risk_decision.to_risk_assessment()
    change_plan = build_change_plan_from_risk_decision(state, risk_decision)
    trace_service.record_risk_decision(
        trace_id=state.get("trace_id") or "trace-unknown",
        incident_id=extract_incident_id(state),
        step_id=risk_decision.step_id,
        action=risk_decision.action,
        policy=risk_decision.policy,
        risk_level=risk_decision.risk_level,
        reason=risk_decision.reason,
        matched_rules=risk_decision.matched_rules,
        status="blocked",
    )
    update: dict[str, Any] = {
        "risk_assessment": risk_assessment.model_dump(mode="json"),
    }

    if risk_decision.policy == "forbidden":
        update["pending_approval"] = None
        update["response"] = _generate_forbidden_response(risk_decision)
        update["errors"] = [risk_decision.reason]
        update["change_plan"] = change_plan.model_dump(mode="json")
        return update

    approval = create_approval_request_from_risk_decision(
        state,
        risk_decision,
        approval_repository=approval_service,
        change_plan=change_plan,
    )
    update["pending_approval"] = approval.model_dump(mode="json")
    update["change_plan"] = change_plan.model_dump(mode="json")
    update["response"] = _generate_approval_waiting_response(update)
    return update


def _extract_risk_decision(state: PlanExecuteState) -> RiskControlDecision | None:
    """Infer risk from remaining structured plan steps."""
    registry = create_default_tool_registry([])
    for raw_step in state.get("current_plan", []):
        try:
            step = raw_step if isinstance(raw_step, PlanStep) else PlanStep(**raw_step)
        except Exception:
            continue
        decision = assess_plan_step(
            step,
            tool_registry=registry,
            incident=state.get("incident"),
        )
        if decision.policy != "allow":
            return decision
    return None


def _record_replanner_decision(
    state: PlanExecuteState,
    decision: ReplanDecision,
    analysis: EvidenceAnalysis,
    *,
    decision_source: str = "evidence_analyzer",
) -> None:
    """Write the structured Replanner decision into trace storage."""
    trace_service.create_event(
        trace_id=state.get("trace_id") or "trace-unknown",
        incident_id=extract_incident_id(state),
        node_name="replanner",
        event_type="replan_decision",
        input_summary=f"hypotheses={len(analysis.hypotheses)}, confidence={analysis.confidence:.2f}",
        output_summary=f"decision={decision.decision}, reason={decision.reason}",
        status="success",
        metadata={
            "decision": decision.decision,
            "reason": decision.reason,
            "decision_source": decision_source,
            "analysis_decision": analysis.decision,
            "new_steps": [step.model_dump(mode="json") for step in decision.new_steps],
            "evidence_sufficient": analysis.evidence_sufficient,
            "missing_evidence": analysis.missing_evidence,
            "conflicts": analysis.conflicts,
            "evidence_profile": analysis.evidence_profile,
            "confidence_reasons": analysis.confidence_reasons,
        },
    )


def _generate_approval_waiting_response(state_update: dict[str, Any]) -> str:
    """Compatibility wrapper for the shared approval pause renderer."""
    return generate_approval_waiting_response(state_update)


def _generate_forbidden_response(decision: RiskControlDecision) -> str:
    """Compatibility wrapper for the shared forbidden-action renderer."""
    return generate_forbidden_response(decision)


async def _generate_response_with_analysis(
    state: PlanExecuteState,
    analysis: EvidenceAnalysis,
    status: str = "completed",
) -> dict[str, Any]:
    """Generate a final response with evidence analysis injected into state."""
    response_state: dict[str, Any] = dict(state)
    response_state["hypotheses"] = analysis.hypotheses
    response_state["evidence_analysis"] = analysis.model_dump(mode="json")
    try:
        response_update = await _generate_response(response_state, _create_llm())
    except Exception as exc:
        logger.warning(f"LLM 响应生成不可用，改用确定性 Report Generator: {exc}")
        response_update = {"response": ""}
    if response_update.get("response"):
        response_state["llm_narrative"] = response_update["response"]
    report = report_generator.generate_from_state(response_state, status=status)
    return {
        "response": report.markdown,
        "report": report.model_dump(mode="json"),
        "hypotheses": analysis.hypotheses,
        "final_diagnosis": report.root_cause,
        "remediation_suggestion": report.remediation_suggestion,
    }


def _with_generated_report(
    state: PlanExecuteState,
    update: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    """Attach a structured report to a terminal Replanner update."""
    report_state = dict(state)
    report_state.update(update)
    report = report_generator.generate_from_state(report_state, status=status)
    update["report"] = report.model_dump(mode="json")
    update.setdefault("final_diagnosis", report.root_cause)
    update.setdefault("remediation_suggestion", report.remediation_suggestion)
    return update


def _infer_report_status(update: dict[str, Any]) -> str:
    """Infer report lifecycle status from a terminal state update."""
    return infer_terminal_report_status(update)


def _create_llm() -> ChatQwen:
    """Create the deterministic LLM used by Replanner response generation."""
    return ChatQwen(
        model=config.effective_rag_model,
        api_key=cast(Any, config.dashscope_api_key),
        base_url=config.dashscope_api_base,
        temperature=0,
    )


def _generate_escalation_response(
    state: PlanExecuteState,
    analysis: EvidenceAnalysis,
) -> str:
    """Generate a deterministic response when the agent should stop and escalate."""
    return f"""# AIOps 诊断需要人工介入

## 原因
{analysis.reason}

## 已形成假设
{_format_list(analysis.hypotheses)}

## 缺失证据
{_format_list(analysis.missing_evidence)}

## 已执行步骤
{_format_simple_steps(state.get("past_steps", []))}
"""


async def _generate_response(state: dict[str, Any], llm: ChatQwen) -> dict[str, Any]:
    """Generate an optional LLM narrative from collected execution facts."""
    logger.info("生成最终响应...")

    input_text = state.get("input", "")
    past_steps = state.get("past_steps", [])
    gathered_evidence = state.get("gathered_evidence", [])
    tool_call_records = state.get("tool_call_records", [])
    hypotheses = state.get("hypotheses", [])
    evidence_analysis = state.get("evidence_analysis")

    execution_history = "\n\n".join(
        [f"### 步骤: {step}\n**结果:**\n{result}" for step, result in past_steps]
    )
    evidence_history = _format_evidence_for_prompt(gathered_evidence)
    tool_call_history = _format_tool_calls_for_prompt(tool_call_records)
    analysis_summary = (
        render_analysis_summary(EvidenceAnalysis(**evidence_analysis))
        if isinstance(evidence_analysis, dict)
        else ""
    )

    response_gen = response_prompt | llm.with_structured_output(Response)

    try:
        messages = [
            ("user", f"原始任务: {input_text}"),
            ("user", f"执行历史:\n{execution_history}"),
            ("user", f"结构化证据:\n{evidence_history}"),
            ("user", f"工具调用记录:\n{tool_call_history}"),
            ("user", f"根因假设:\n{_format_list(hypotheses)}"),
            ("user", f"证据分析:\n{analysis_summary}"),
            ("user", "请基于以上信息生成全面的最终响应"),
        ]

        response_obj = await response_gen.ainvoke({"messages": messages})

        if isinstance(response_obj, Response):
            final_response = response_obj.response
        elif isinstance(response_obj, dict):
            final_response = str(response_obj.get("response", ""))
        else:
            final_response = ""

        logger.info(f"最终响应生成完成，长度: {len(final_response)}")

        return {"response": final_response}

    except Exception as e:
        logger.error(f"生成响应失败: {e}")
        fallback_response = f"""# 任务执行结果

## 原始任务
{input_text}

## 执行的步骤
{_format_simple_steps(past_steps)}

## 结构化证据
{evidence_history}

## 根因假设
{_format_list(hypotheses)}

## 说明
由于系统异常，无法生成完整响应。以上是已收集的信息。
"""
        return {"response": fallback_response}


def _format_simple_steps(past_steps: list) -> str:
    """Render compact step history for deterministic fallback messages."""
    if not past_steps:
        return "无"

    formatted = []
    for i, (step, result) in enumerate(past_steps, 1):
        result_preview = result[:200] + "..." if len(result) > 200 else result
        formatted.append(f"{i}. **{step}**\n   {result_preview}\n")

    return "\n".join(formatted)


def _format_evidence_for_prompt(gathered_evidence: list) -> str:
    """Format structured evidence for final report generation."""
    if not gathered_evidence:
        return "无"

    formatted = []
    for index, evidence in enumerate(gathered_evidence, 1):
        if not isinstance(evidence, dict):
            continue
        raw_data = evidence.get("raw_data") or {}
        status = raw_data.get("status", "unknown") if isinstance(raw_data, dict) else "unknown"
        formatted.append(
            "\n".join(
                [
                    f"{index}. 工具: {evidence.get('source_tool', 'unknown')}",
                    f"   步骤: {evidence.get('step_id', '')}",
                    f"   状态: {status}",
                    f"   摘要: {evidence.get('summary', '')}",
                    f"   类型: {evidence.get('evidence_type', 'unknown')}",
                    f"   立场: {evidence.get('stance', 'neutral')}",
                    f"   置信度: {evidence.get('confidence', 0)}",
                    f"   置信度原因: {evidence.get('confidence_reason', '')}",
                ]
            )
        )
    return "\n".join(formatted) if formatted else "无"


def _format_tool_calls_for_prompt(tool_call_records: list) -> str:
    """Format tool call audit records for final report generation."""
    if not tool_call_records:
        return "无"

    formatted = []
    for index, record in enumerate(tool_call_records, 1):
        if not isinstance(record, dict):
            continue
        formatted.append(
            f"{index}. {record.get('tool_name', 'unknown')} "
            f"step={record.get('step_id', '')} "
            f"status={record.get('status', '')} "
            f"latency_ms={record.get('latency_ms', 0)} "
            f"error={record.get('error_message') or ''}"
        )
    return "\n".join(formatted) if formatted else "无"


def _format_list(items: list[str]) -> str:
    """Render a bullet list with a stable empty value."""
    return "\n".join(f"- {item}" for item in items) if items else "- 无"
