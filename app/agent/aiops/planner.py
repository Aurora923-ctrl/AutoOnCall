"""
Planner 节点：制定执行计划
基于 LangGraph 官方教程实现
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from textwrap import dedent
from typing import Any, cast

import yaml
from langchain_core.prompts import ChatPromptTemplate
from langchain_qwq import ChatQwen
from loguru import logger
from pydantic import BaseModel, Field

from app.agent.mcp_client import get_mcp_client_with_retry
from app.config import config
from app.models.evidence import Evidence
from app.models.plan import PlanStep
from app.services.rag_read_models import compact_retrieval_payload
from app.services.rag_retrieval_service import retrieve_structured_knowledge
from app.tools import get_current_time, retrieve_knowledge
from app.tools.registry import create_default_tool_registry
from app.utils.log_safety import summarize_text_for_log

from .plan_fallback import (
    append_incident_requested_action_step,
    build_fallback_plan,
    build_golden_dependency_plan,
    infer_dependency_hint,
    infer_service_name,
    infer_symptom,
    normalize_plan_steps,
)
from .state import PlanExecuteState, normalize_plan_state_update
from .utils import format_tools_description


class Plan(BaseModel):
    """计划的输出格式"""

    steps: list[PlanStep] = Field(
        description="完成任务所需的结构化排障步骤。每个步骤必须包含 tool_name、purpose、input_args、expected_evidence、risk_level 和 status。"
    )


@dataclass(slots=True)
class PlannerDependencies:
    """Optional dependency overrides for tests and alternative planner runtimes."""

    knowledge_retriever: Callable[[str], dict[str, Any]] | None = None
    mcp_client_factory: Callable[[], Awaitable[Any]] | None = None
    llm_factory: Callable[[], Any] | None = None
    tool_registry_factory: Callable[[list[Any]], Any] | None = None


planner_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            dedent("""
                作为一个专家级别的规划者，你需要将复杂的任务分解为可执行的步骤。

                可用工具列表（用于制定计划时参考）：

                {tools_description}

                注意：你的职责是制定计划，实际的工具调用由 Executor 负责执行。

                {experience_context}

                对于给定的任务，请创建一个简单的、逐步的结构化计划来完成它。计划应该：
                - 将任务分解为逻辑上独立的步骤
                - 每个步骤必须明确 tool_name 和 input_args，让 Executor 可以稳定消费
                - step_id 使用 s1、s2、s3 这样的稳定编号
                - risk_level 只能是 low、medium、high
                - status 固定为 pending
                - 步骤之间应该有清晰的依赖关系
                - 步骤描述要具体、可操作
                - **如果有相关经验文档，请参考其中的方法和步骤制定计划**
                - 如果经验文档包含 symptoms、diagnosis_steps、risk_actions 等结构化字段，
                  优先把 diagnosis_steps 转为只读排查步骤；risk_actions 只能作为建议或审批候选，不能自动执行。
                - 对服务不可用、慢响应、timeout、下游依赖异常类事件，优先使用
                  query_metrics、query_logs、query_service_context、query_deploy_history
                  以及 Redis/MySQL/Kubernetes 状态工具采集证据。

                标准工具名优先使用：
                {standard_tool_names}

                示例输入："分析当前系统的性能问题"
                示例输出：
                {{
                  "steps": [
                    {{
                      "step_id": "s1",
                      "tool_name": "query_metrics",
                      "purpose": "检查 order-service 最近 10 分钟 QPS、P95、错误率、CPU 和内存",
                      "input_args": {{"service_name": "order-service", "time_range": "10m", "interval": "1m"}},
                      "expected_evidence": "确认服务是否存在流量、延迟、错误率或资源异常",
                      "risk_level": "low",
                      "status": "pending",
                      "retry_count": 0
                    }}
                  ]
                }}
            """).strip(),
        ),
        ("placeholder", "{messages}"),
    ]
)


def _create_planner_llm() -> Any:
    """Create the default structured-output LLM used by the planner."""
    return ChatQwen(
        model=config.effective_rag_model,
        api_key=cast(Any, config.dashscope_api_key),
        base_url=config.dashscope_api_base,
        temperature=0,
    )


async def _call_knowledge_retriever(
    knowledge_retriever: Callable[[str], dict[str, Any]],
    query: str,
) -> dict[str, Any]:
    """Call the sync retrieval stack without blocking the event loop."""
    result = await asyncio.to_thread(knowledge_retriever, query)
    if inspect.isawaitable(result):
        result = await result
    return dict(result or {})


async def planner(
    state: PlanExecuteState,
    dependencies: PlannerDependencies | None = None,
) -> dict[str, Any]:
    """
    规划节点：根据用户输入生成执行计划

    流程：
    1. 先查询内部文档，获取相关经验和最佳实践
    2. 基于经验文档和可用工具制定执行计划
    """
    logger.info("=== Planner：制定执行计划 ===")
    dependencies = dependencies or PlannerDependencies()
    knowledge_retriever = dependencies.knowledge_retriever or retrieve_structured_knowledge
    mcp_client_factory = dependencies.mcp_client_factory or get_mcp_client_with_retry
    llm_factory = dependencies.llm_factory or _create_planner_llm
    tool_registry_factory = dependencies.tool_registry_factory or create_default_tool_registry

    input_text = state.get("input", "")
    incident = state.get("incident", {})
    retrieval_query = _build_planner_retrieval_query(input_text, incident)
    runbook_evidence: list[dict[str, Any]] = []
    planner_warnings: list[str] = []
    logger.info(f"用户输入: {summarize_text_for_log(input_text, label='aiops_input')}")
    logger.info(
        f"Runbook 检索查询: {summarize_text_for_log(retrieval_query, label='retrieval_query')}"
    )

    try:
        logger.info("查询内部文档，寻找相关经验...")
        experience_docs = ""
        try:
            retrieval_payload = await _call_knowledge_retriever(
                knowledge_retriever,
                retrieval_query,
            )
            if retrieval_payload.get("status") == "success":
                experience_docs = str(retrieval_payload.get("content") or "")
                runbook_evidence.append(
                    Evidence(
                        source_tool="retrieve_knowledge",
                        step_id="planner-runbook",
                        summary=str(
                            retrieval_payload.get("summary") or "Planner 命中 Runbook 证据"
                        ),
                        evidence_type="runbook",
                        data_source="rag",
                        stance="supporting",
                        confidence_reason="Runbook 检索命中，用于约束诊断计划",
                        fact=str(retrieval_payload.get("summary") or "Planner 命中 Runbook 证据"),
                        inference="Runbook 命中结果用于约束诊断计划，但仍需工具证据验证现场状态。",
                        uncertainty="知识库内容可能滞后，不能替代实时指标、日志和依赖状态。",
                        next_step="按计划调用只读工具采集实时证据。",
                        raw_data={"output": compact_retrieval_payload(retrieval_payload)},
                        confidence=0.65,
                        related_hypothesis="Runbook knowledge used for planning",
                    ).model_dump(mode="json")
                )
                logger.info(f"找到相关经验文档，长度: {len(experience_docs)}")
            else:
                logger.info(f"未找到相关经验文档: status={retrieval_payload.get('status')}")
        except Exception as e:
            logger.warning(f"查询内部文档失败: {e}")

        local_tools = [get_current_time, retrieve_knowledge]
        mcp_tools: list[Any] = []
        try:
            mcp_client = await mcp_client_factory()
            mcp_tools = await mcp_client.get_tools()
        except Exception as exc:
            warning = "MCP 工具发现失败，Planner 已降级使用本地和标准工具契约继续规划。"
            planner_warnings.append(warning)
            logger.warning(f"{warning} error={exc}")

        all_tools = local_tools + mcp_tools
        logger.info(f"可用工具数量: 本地 {len(local_tools)} + MCP {len(mcp_tools)}")

        tool_contracts = tool_registry_factory(all_tools).list_contracts()
        tools_description = format_tools_description(tool_contracts)
        standard_tool_names = [contract.name for contract in tool_contracts]
        runbook_steps = _extract_runbook_sop_steps(experience_docs, input_text, incident)

        if experience_docs:
            experience_context = dedent(f"""
                ## 相关经验文档

                以下是从知识库中检索到的相关经验和最佳实践，请参考这些经验制定执行计划：

                {experience_docs}

                ---
            """).strip()
        else:
            experience_context = ""

        llm = llm_factory()

        planner_chain = planner_prompt | llm.with_structured_output(Plan)

        plan_result = await planner_chain.ainvoke(
            {
                "messages": [("user", input_text)],
                "tools_description": tools_description,
                "experience_context": experience_context,
                "standard_tool_names": "\n".join(f"- {name}" for name in standard_tool_names),
            }
        )

        if isinstance(plan_result, Plan):
            raw_steps = plan_result.steps
        elif isinstance(plan_result, dict):
            raw_steps = plan_result.get("steps", [])
        else:
            raw_steps = []

        structured_steps = _stabilize_interview_golden_plan(
            _merge_runbook_steps(
                runbook_steps,
                normalize_plan_steps(raw_steps, input_text, incident),
            ),
            input_text=input_text,
            incident=incident,
        )
        logger.info(f"结构化计划已生成，共 {len(structured_steps)} 个步骤")
        for i, step in enumerate(structured_steps, 1):
            logger.info(
                f"  步骤{i}: tool={step.tool_name}, risk={step.risk_level}, "
                f"{summarize_text_for_log(step.purpose, label='purpose')}"
            )

        return {
            **normalize_plan_state_update(structured_steps),
            "gathered_evidence": runbook_evidence,
            "warnings": planner_warnings,
        }

    except Exception as e:
        logger.error(f"生成计划失败: {e}", exc_info=True)
        structured_steps = _stabilize_interview_golden_plan(
            _merge_runbook_steps(
                _extract_runbook_sop_steps(
                    locals().get("experience_docs", ""), input_text, incident
                ),
                build_fallback_plan(input_text=input_text, incident=incident),
            ),
            input_text=input_text,
            incident=incident,
        )
        return {
            **normalize_plan_state_update(structured_steps),
            "gathered_evidence": runbook_evidence,
            "warnings": planner_warnings,
        }


def _extract_runbook_sop_steps(
    experience_docs: str,
    input_text: str,
    incident: dict[str, Any],
) -> list[PlanStep]:
    """Convert optional Runbook diagnosis_steps metadata into candidate plan steps."""
    if not experience_docs.strip():
        return []
    payload = _parse_runbook_metadata(experience_docs)
    diagnosis_steps = payload.get("diagnosis_steps")
    if not isinstance(diagnosis_steps, list):
        return []
    service_name = str(incident.get("service_name") or "unknown-service")
    steps: list[PlanStep] = []
    for item in diagnosis_steps[:3]:
        text = str(item).strip()
        if not text:
            continue
        tool_name = _infer_runbook_step_tool(text)
        steps.append(
            PlanStep(
                step_id=f"rb{len(steps) + 1}",
                tool_name=tool_name,
                purpose=f"[Runbook] {text}",
                input_args=_runbook_step_args(tool_name, service_name, input_text),
                expected_evidence="来自结构化 Runbook diagnosis_steps，需要实时工具证据验证。",
                risk_level="low",
                status="pending",
            )
        )
    return steps


def _parse_runbook_metadata(experience_docs: str) -> dict[str, Any]:
    """Best-effort parse of YAML metadata embedded in retrieved Runbook text."""
    candidates = [experience_docs]
    if "---" in experience_docs:
        parts = [part.strip() for part in experience_docs.split("---") if part.strip()]
        candidates = parts + candidates
    for candidate in candidates:
        try:
            payload = yaml.safe_load(candidate)
        except Exception:
            continue
        if isinstance(payload, dict) and any(
            key in payload for key in ["symptoms", "diagnosis_steps", "risk_actions"]
        ):
            return payload
    return {}


def _infer_runbook_step_tool(text: str) -> str:
    lowered = text.lower()
    if "redis" in lowered:
        return "query_redis_status"
    if "mysql" in lowered or "sql" in lowered:
        return "query_mysql_status"
    if "pod" in lowered or "k8s" in lowered or "kubernetes" in lowered:
        return "query_k8s_status"
    if "log" in lowered or "日志" in lowered:
        return "query_logs"
    if "metric" in lowered or "指标" in lowered or "p95" in lowered:
        return "query_metrics"
    return "manual_analysis"


def _runbook_step_args(tool_name: str, service_name: str, input_text: str) -> dict[str, Any]:
    if tool_name == "query_logs":
        return {"service_name": service_name, "time_range": "10m", "query": "ERROR OR timeout"}
    if tool_name == "query_metrics":
        return {"service_name": service_name, "time_range": "10m", "interval": "1m"}
    if tool_name == "manual_analysis":
        return {"service_name": service_name, "task": input_text}
    return {"service_name": service_name, "time_range": "10m"}


def _build_planner_retrieval_query(input_text: str, incident: Any) -> str:
    """Build a Runbook retrieval query centered on the incident, not the report template."""
    if not isinstance(incident, dict) or not incident:
        return input_text

    parts = [
        str(incident.get("title") or ""),
        str(incident.get("service_name") or ""),
        str(incident.get("severity") or ""),
        str(incident.get("symptom") or ""),
        str(incident.get("environment") or ""),
    ]
    raw_alert = incident.get("raw_alert")
    if isinstance(raw_alert, dict):
        for key in [
            "alertname",
            "dependency",
            "topic",
            "consumer_group",
            "requested_action",
            "reason",
            "description",
        ]:
            value = raw_alert.get(key)
            if value:
                parts.append(str(value))
        for key, value in raw_alert.items():
            if key in {"sql"}:
                continue
            if isinstance(value, str | int | float | bool):
                parts.append(f"{key}={value}")

    query = " ".join(part.strip() for part in parts if part and part.strip())
    return query or input_text


def _merge_runbook_steps(
    runbook_steps: list[PlanStep],
    model_steps: list[PlanStep],
) -> list[PlanStep]:
    """Prepend unique Runbook SOP steps without dropping model/fallback coverage."""
    if not runbook_steps:
        return model_steps
    seen: set[str] = set()
    merged: list[PlanStep] = []
    for step in runbook_steps + model_steps:
        key = f"{step.tool_name}:{step.input_args}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(step)
    return [
        step.model_copy(update={"step_id": f"s{index}", "status": "pending"})
        for index, step in enumerate(merged, 1)
    ]


def _stabilize_interview_golden_plan(
    steps: list[PlanStep],
    *,
    input_text: str,
    incident: dict[str, Any],
) -> list[PlanStep]:
    """Keep interview golden incidents on a stable evidence and risk path."""
    raw_alert = incident.get("raw_alert")
    if not isinstance(raw_alert, dict):
        return steps

    service_name = infer_service_name(input_text, incident)
    symptom = infer_symptom(input_text, incident)
    dependency_hint = infer_dependency_hint(service_name, symptom.lower())
    golden_steps = build_golden_dependency_plan(
        service_name,
        symptom,
        dependency_hint,
        has_raw_alert=True,
    )
    if not golden_steps:
        return steps

    by_tool = {step.tool_name: step for step in steps}
    stabilized = [by_tool.get(step.tool_name, step) for step in golden_steps]
    stabilized = append_incident_requested_action_step(stabilized, incident)
    return [
        step.model_copy(update={"step_id": f"s{index}", "status": "pending"})
        for index, step in enumerate(stabilized, 1)
    ]
