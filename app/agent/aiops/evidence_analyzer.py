"""Evidence analysis helpers for AIOps Replanner decisions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.hypothesis import RootCauseHypothesis
from app.models.plan import PlanStep
from app.services.diagnostic_signal_rules import (
    DIAGNOSTIC_SIGNAL_CANDIDATES,
    build_signal_hypotheses,
    dedupe_strings,
    evidence_matches_category,
    evidence_output,
    has_k8s_oom_signal,
    has_mysql_signal,
    infer_evidence_type,
    mentions_any,
    missing_tools_from_context,
    signal_context,
)

from .state import PlanExecuteState

AnalyzerDecision = Literal[
    "continue_investigation",
    "add_steps",
    "retry_failed_tool",
    "request_approval",
    "generate_report",
    "escalate_to_human",
]


class EvidenceAnalysis(BaseModel):
    """Structured analysis derived from collected evidence and tool calls."""

    decision: AnalyzerDecision = "continue_investigation"
    reason: str = ""
    hypotheses: list[str] = Field(default_factory=list)
    hypothesis_ranking: list[RootCauseHypothesis] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    evidence_sufficient: bool = False
    missing_evidence: list[str] = Field(default_factory=list)
    recommended_steps: list[PlanStep] = Field(default_factory=list)
    retry_steps: list[PlanStep] = Field(default_factory=list)
    evidence_profile: dict[str, Any] = Field(default_factory=dict)
    confidence_reasons: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


def analyze_evidence(state: PlanExecuteState) -> EvidenceAnalysis:
    """Analyze structured evidence and propose the next Replanner decision."""
    evidence_items = _as_list(state.get("gathered_evidence", []))
    tool_records = _as_list(state.get("tool_call_records", []))
    current_plan = _as_list(state.get("current_plan", []))
    plan = _as_list(state.get("plan", []))
    incident = state.get("incident") or {}
    input_text = str(state.get("input", ""))
    service_name = _extract_service_name(incident)

    successful_tools = _successful_tools(evidence_items)
    failed_records_all = _failed_tool_records(tool_records, evidence_items)
    failed_records = _retryable_failed_records(failed_records_all, successful_tools)
    exhausted_failed_records = _exhausted_failed_records(failed_records_all, successful_tools)
    missing_tools = _missing_tools(evidence_items, input_text, incident)
    hypotheses = _build_hypotheses(evidence_items, input_text, incident)
    hypothesis_ranking = _build_hypothesis_ranking(
        evidence_items=evidence_items,
        input_text=input_text,
        incident=incident,
        missing_tools=missing_tools,
    )
    if hypothesis_ranking:
        hypotheses = dedupe_strings([item.title for item in hypothesis_ranking] + hypotheses)
    retry_steps = _build_retry_steps(failed_records)
    recommended_steps = _build_recommended_steps(missing_tools, service_name)
    evidence_profile = _build_evidence_profile(evidence_items, tool_records)
    conflicts = _detect_conflicts(evidence_items, input_text, incident)
    confidence_reasons = _build_confidence_reasons(
        evidence_items, conflicts, exhausted_failed_records
    )

    has_remaining_plan = bool(current_plan or plan)
    sufficient, confidence, reason = _judge_sufficiency(
        hypotheses,
        successful_tools,
        conflicts,
        exhausted_failed_records,
    )

    if retry_steps:
        return EvidenceAnalysis(
            decision="retry_failed_tool",
            reason=f"检测到失败工具调用，需要重试: {retry_steps[0].tool_name}",
            hypotheses=hypotheses,
            hypothesis_ranking=hypothesis_ranking,
            conflicts=conflicts,
            evidence_sufficient=False,
            missing_evidence=missing_tools,
            recommended_steps=recommended_steps,
            retry_steps=retry_steps,
            evidence_profile=evidence_profile,
            confidence_reasons=confidence_reasons,
            confidence=confidence,
        )

    if exhausted_failed_records and hypotheses:
        failed_tools = ", ".join(
            dedupe_strings([str(item.get("tool_name", "")) for item in exhausted_failed_records])
        )
        return EvidenceAnalysis(
            decision="generate_report",
            reason=f"失败工具已重试或不可恢复，基于已有证据降级生成不完整诊断: {failed_tools}",
            hypotheses=hypotheses,
            hypothesis_ranking=hypothesis_ranking,
            conflicts=conflicts,
            evidence_sufficient=False,
            missing_evidence=missing_tools,
            recommended_steps=[],
            retry_steps=[],
            evidence_profile=evidence_profile,
            confidence_reasons=confidence_reasons,
            confidence=confidence,
        )

    if conflicts and hypotheses:
        return EvidenceAnalysis(
            decision="generate_report",
            reason=f"检测到证据冲突，生成待确认报告: {conflicts[0]}",
            hypotheses=hypotheses,
            hypothesis_ranking=hypothesis_ranking,
            conflicts=conflicts,
            evidence_sufficient=False,
            missing_evidence=missing_tools,
            recommended_steps=[],
            retry_steps=[],
            evidence_profile=evidence_profile,
            confidence_reasons=confidence_reasons,
            confidence=confidence,
        )

    if sufficient:
        return EvidenceAnalysis(
            decision="generate_report",
            reason=reason,
            hypotheses=hypotheses,
            hypothesis_ranking=hypothesis_ranking,
            conflicts=conflicts,
            evidence_sufficient=True,
            missing_evidence=missing_tools,
            evidence_profile=evidence_profile,
            confidence_reasons=confidence_reasons,
            confidence=confidence,
        )

    if recommended_steps and not has_remaining_plan:
        return EvidenceAnalysis(
            decision="add_steps",
            reason="当前证据不足，且没有剩余计划，需要追加关键证据采集步骤",
            hypotheses=hypotheses,
            hypothesis_ranking=hypothesis_ranking,
            conflicts=conflicts,
            evidence_sufficient=False,
            missing_evidence=missing_tools,
            recommended_steps=recommended_steps,
            evidence_profile=evidence_profile,
            confidence_reasons=confidence_reasons,
            confidence=confidence,
        )

    if has_remaining_plan:
        return EvidenceAnalysis(
            decision="continue_investigation",
            reason="当前证据不足，但仍有剩余计划可继续执行",
            hypotheses=hypotheses,
            hypothesis_ranking=hypothesis_ranking,
            conflicts=conflicts,
            evidence_sufficient=False,
            missing_evidence=missing_tools,
            recommended_steps=recommended_steps,
            evidence_profile=evidence_profile,
            confidence_reasons=confidence_reasons,
            confidence=confidence,
        )

    return EvidenceAnalysis(
        decision="escalate_to_human",
        reason="没有足够证据，也没有可追加的安全只读排查步骤",
        hypotheses=hypotheses,
        hypothesis_ranking=hypothesis_ranking,
        conflicts=conflicts,
        evidence_sufficient=False,
        missing_evidence=missing_tools,
        evidence_profile=evidence_profile,
        confidence_reasons=confidence_reasons,
        confidence=confidence,
    )


def render_analysis_summary(analysis: EvidenceAnalysis) -> str:
    """Render analysis into compact text for prompts and reports."""
    hypotheses = "\n".join(f"- {item}" for item in analysis.hypotheses) or "- 暂无明确假设"
    ranked = (
        "\n".join(
            f"- {item.title} confidence={item.confidence:.2f}: {item.confidence_reason}"
            for item in analysis.hypothesis_ranking[:5]
        )
        or "- 暂无"
    )
    missing = "\n".join(f"- {item}" for item in analysis.missing_evidence) or "- 暂无"
    conflicts = "\n".join(f"- {item}" for item in analysis.conflicts) or "- 暂无"
    reasons = "\n".join(f"- {item}" for item in analysis.confidence_reasons) or "- 暂无"
    return (
        f"决策: {analysis.decision}\n"
        f"原因: {analysis.reason}\n"
        f"置信度: {analysis.confidence:.2f}\n"
        f"证据是否充足: {'是' if analysis.evidence_sufficient else '否'}\n"
        f"根因假设:\n{hypotheses}\n"
        f"根因假设排序:\n{ranked}\n"
        f"证据冲突:\n{conflicts}\n"
        f"缺失证据:\n{missing}\n"
        f"置信度原因:\n{reasons}"
    )


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _extract_service_name(incident: Any) -> str:
    if isinstance(incident, dict):
        return str(incident.get("service_name") or "unknown-service")
    return str(getattr(incident, "service_name", "unknown-service"))


def _successful_tools(evidence_items: list[Any]) -> set[str]:
    tools: set[str] = set()
    for evidence in evidence_items:
        if not isinstance(evidence, dict):
            continue
        raw_data = evidence.get("raw_data") or {}
        if raw_data.get("status") == "success":
            tools.add(str(evidence.get("source_tool") or raw_data.get("tool_name") or ""))
    return {tool for tool in tools if tool}


def _failed_tool_records(
    tool_records: list[Any], evidence_items: list[Any]
) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []

    for record in tool_records:
        if isinstance(record, dict) and record.get("status") == "failed":
            failed.append(record)

    for evidence in evidence_items:
        if not isinstance(evidence, dict):
            continue
        raw_data = evidence.get("raw_data") or {}
        if raw_data.get("status") == "failed":
            failed.append(
                {
                    "step_id": evidence.get("step_id", ""),
                    "tool_name": evidence.get("source_tool") or raw_data.get("tool_name", ""),
                    "input_args": raw_data.get("input_args", {}),
                    "error_message": raw_data.get("error_message"),
                }
            )

    return _dedupe_failed_records(failed)


def _dedupe_failed_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for record in records:
        key = (str(record.get("step_id", "")), str(record.get("tool_name", "")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def _retryable_failed_records(
    records: list[dict[str, Any]],
    successful_tools: set[str],
) -> list[dict[str, Any]]:
    retried_tools = {
        str(record.get("tool_name") or "")
        for record in records
        if str(record.get("step_id") or "").endswith("-retry")
    }
    return [
        record
        for record in records
        if str(record.get("tool_name") or "") not in successful_tools
        and str(record.get("tool_name") or "") not in retried_tools
    ]


def _exhausted_failed_records(
    records: list[dict[str, Any]],
    successful_tools: set[str],
) -> list[dict[str, Any]]:
    """Return failed tool records that should no longer be retried."""
    retry_failed_tools = {
        str(record.get("tool_name") or "")
        for record in records
        if str(record.get("step_id") or "").endswith("-retry")
    }
    exhausted = [
        record
        for record in records
        if str(record.get("tool_name") or "") in retry_failed_tools
        and str(record.get("tool_name") or "") not in successful_tools
    ]
    return _dedupe_failed_records(exhausted)


def _build_hypotheses(
    evidence_items: list[Any],
    input_text: str,
    incident: Any,
) -> list[str]:
    return build_signal_hypotheses(evidence_items, input_text, incident)


def _build_hypothesis_ranking(
    *,
    evidence_items: list[Any],
    input_text: str,
    incident: Any,
    missing_tools: list[str],
) -> list[RootCauseHypothesis]:
    """Build a scenario-aware root-cause matrix from evidence and incident text."""
    context = signal_context(evidence_items, input_text, incident)

    ranking: list[RootCauseHypothesis] = []
    for candidate in DIAGNOSTIC_SIGNAL_CANDIDATES:
        category = str(candidate["category"])
        keywords = [str(item) for item in candidate["keywords"]]
        tools = [str(item) for item in candidate["tools"]]
        supporting = _matching_evidence_ids(evidence_items, category, keywords, "supporting")
        refuting = _matching_evidence_ids(evidence_items, category, keywords, "refuting")
        mentioned = mentions_any(context, keywords)
        if not mentioned and not supporting and not refuting:
            continue
        missing = [tool for tool in tools if tool in missing_tools]
        confidence = _score_hypothesis(
            mentioned=mentioned,
            supporting_count=len(supporting),
            refuting_count=len(refuting),
            missing_count=len(missing),
        )
        ranking.append(
            RootCauseHypothesis(
                title=str(candidate["title"]),
                description=str(candidate["title"]),
                category=category,
                supporting_evidence_ids=supporting,
                refuting_evidence_ids=refuting,
                missing_evidence=missing,
                confidence=confidence,
                confidence_reason=_hypothesis_confidence_reason(
                    mentioned=mentioned,
                    supporting_count=len(supporting),
                    refuting_count=len(refuting),
                    missing=missing,
                ),
            )
        )

    return sorted(ranking, key=lambda item: item.confidence, reverse=True)


def _matching_evidence_ids(
    evidence_items: list[Any],
    category: str,
    keywords: list[str],
    stance: str,
) -> list[str]:
    ids: list[str] = []
    for index, evidence in enumerate(evidence_items, 1):
        if not isinstance(evidence, dict):
            continue
        if str(evidence.get("stance") or "neutral") != stance:
            continue
        evidence_type = _type_from_tool(evidence)
        text = f"{evidence.get('summary', '')} {evidence_output(evidence)}".lower()
        if evidence_matches_category(category, evidence_type, text, keywords):
            ids.append(
                str(evidence.get("evidence_id") or f"{evidence.get('source_tool', 'tool')}-{index}")
            )
    return dedupe_strings(ids)


def _score_hypothesis(
    *,
    mentioned: bool,
    supporting_count: int,
    refuting_count: int,
    missing_count: int,
) -> float:
    score = 0.22 if mentioned else 0.12
    score += min(0.54, supporting_count * 0.18)
    score -= min(0.3, refuting_count * 0.15)
    score -= min(0.18, missing_count * 0.06)
    return _bounded_confidence(score)


def _hypothesis_confidence_reason(
    *,
    mentioned: bool,
    supporting_count: int,
    refuting_count: int,
    missing: list[str],
) -> str:
    parts: list[str] = []
    if mentioned:
        parts.append("症状或证据文本命中该场景关键词")
    if supporting_count:
        parts.append(f"{supporting_count} 条支持证据")
    if refuting_count:
        parts.append(f"{refuting_count} 条反驳证据降低置信度")
    if missing:
        parts.append("缺失关键证据: " + ", ".join(missing))
    return "；".join(parts) or "仅形成弱假设，需要补充证据"


def _missing_tools(evidence_items: list[Any], input_text: str, incident: Any) -> list[str]:
    successful = _successful_tools(evidence_items)
    context = " ".join(
        [
            input_text,
            str(incident.get("title", "")) if isinstance(incident, dict) else "",
            str(incident.get("symptom", "")) if isinstance(incident, dict) else "",
        ]
    ).lower()
    return missing_tools_from_context(successful, context)


def _build_recommended_steps(missing_tools: list[str], service_name: str) -> list[PlanStep]:
    builders = {
        "query_metrics": lambda: PlanStep(
            step_id="replan-metrics",
            tool_name="query_metrics",
            purpose=f"补充查询 {service_name} 最近 10 分钟的 QPS、P95、错误率、CPU 和内存",
            input_args={"service_name": service_name, "time_range": "10m", "interval": "1m"},
            expected_evidence="确认服务是否存在延迟、错误率或资源异常",
            risk_level="low",
        ),
        "query_logs": lambda: PlanStep(
            step_id="replan-logs",
            tool_name="query_logs",
            purpose=f"补充查询 {service_name} 最近 10 分钟 ERROR 和 timeout 日志",
            input_args={
                "service_name": service_name,
                "time_range": "10m",
                "query": "ERROR OR timeout",
            },
            expected_evidence="确认是否存在 timeout、5xx 或下游依赖异常日志",
            risk_level="low",
        ),
        "query_redis_status": lambda: PlanStep(
            step_id="replan-redis",
            tool_name="query_redis_status",
            purpose="补充查询 Redis connected_clients、maxclients、blocked_clients 和慢日志",
            input_args={"service_name": service_name, "time_range": "10m"},
            expected_evidence="判断 Redis 是否存在连接数耗尽或慢命令异常",
            risk_level="low",
        ),
        "query_mysql_status": lambda: PlanStep(
            step_id="replan-mysql",
            tool_name="query_mysql_status",
            purpose=f"补充查询 {service_name} 相关 MySQL 慢查询、连接池和锁等待",
            input_args={"service_name": service_name, "time_range": "10m"},
            expected_evidence="判断 MySQL 是否存在慢查询、连接池耗尽或锁等待",
            risk_level="low",
        ),
        "query_k8s_status": lambda: PlanStep(
            step_id="replan-k8s",
            tool_name="query_k8s_status",
            purpose=f"补充查询 {service_name} Pod 状态、重启次数和部署版本",
            input_args={"service_name": service_name, "time_range": "10m"},
            expected_evidence="判断 Pod 是否 CrashLoopBackOff、频繁重启或版本异常",
            risk_level="low",
        ),
        "query_message_queue_status": lambda: PlanStep(
            step_id="replan-message-queue",
            tool_name="query_message_queue_status",
            purpose=f"补充查询 {service_name} 关联 Redpanda/Kafka topic、partition 和 consumer lag",
            input_args={
                "service_name": service_name,
                "topic": f"redpanda-{service_name.removesuffix('-service')}",
            },
            expected_evidence="判断消息队列是否存在消费积压、分区异常或 rebalance",
            risk_level="low",
        ),
    }
    return [builders[tool_name]() for tool_name in missing_tools if tool_name in builders]


def _build_retry_steps(failed_records: list[dict[str, Any]]) -> list[PlanStep]:
    retry_steps: list[PlanStep] = []
    for record in failed_records:
        tool_name = str(record.get("tool_name") or "")
        if not tool_name or tool_name == "manual_analysis":
            continue
        step_id = str(record.get("step_id") or "failed-step")
        retry_steps.append(
            PlanStep(
                step_id=f"{step_id}-retry",
                tool_name=tool_name,
                purpose=f"重试失败的工具调用 {tool_name}",
                input_args=dict(record.get("input_args") or {}),
                expected_evidence=f"确认 {tool_name} 是否仍然失败，并补齐对应诊断证据",
                risk_level="low",
                retry_count=1,
            )
        )
    return retry_steps[:1]


def _judge_sufficiency(
    hypotheses: list[str],
    successful_tools: set[str],
    conflicts: list[str],
    exhausted_failed_records: list[dict[str, Any]],
) -> tuple[bool, float, str]:
    has_redis_root_cause = any("Redis" in item and "maxclients" in item for item in hypotheses)
    has_metrics = "query_metrics" in successful_tools
    has_logs = "query_logs" in successful_tools
    has_redis = "query_redis_status" in successful_tools
    has_message_queue_root_cause = any(
        ("Redpanda" in item or "Kafka" in item) and ("积压" in item or "分区" in item)
        for item in hypotheses
    )
    has_message_queue = "query_message_queue_status" in successful_tools

    confidence_penalty = 0.0
    if conflicts:
        confidence_penalty += 0.18
    if exhausted_failed_records:
        confidence_penalty += min(0.2, 0.08 * len(exhausted_failed_records))

    if has_redis_root_cause and has_redis and (has_metrics or has_logs):
        return (
            not conflicts,
            _bounded_confidence(0.86 - confidence_penalty),
            "Redis 关键证据已覆盖，且具备指标或日志侧旁证，可以生成报告",
        )

    if has_message_queue_root_cause and has_message_queue and (has_metrics or has_logs):
        return (
            not conflicts,
            _bounded_confidence(0.82 - confidence_penalty),
            "消息队列积压关键证据已覆盖，且具备指标或日志侧旁证，可以生成报告",
        )

    if len(successful_tools) >= 3 and hypotheses:
        return (
            not conflicts,
            _bounded_confidence(0.72 - confidence_penalty),
            "已收集至少三类成功证据，并形成可解释根因假设",
        )

    if hypotheses:
        return (
            False,
            _bounded_confidence(0.55 - confidence_penalty),
            "已形成初步假设，但关键证据仍不足",
        )

    return False, _bounded_confidence(0.2 - confidence_penalty), "尚未形成可靠根因假设"


def _build_evidence_profile(
    evidence_items: list[Any],
    tool_records: list[Any],
) -> dict[str, Any]:
    """Summarize evidence quality for Replanner and reports."""
    by_type: dict[str, int] = {}
    by_stance: dict[str, int] = {}
    failed_tools = dedupe_strings(
        [
            str(record.get("tool_name") or "")
            for record in tool_records
            if isinstance(record, dict) and record.get("status") == "failed"
        ]
    )
    confidence_values: list[float] = []

    for evidence in evidence_items:
        if not isinstance(evidence, dict):
            continue
        evidence_type = str(evidence.get("evidence_type") or _type_from_tool(evidence))
        stance = str(evidence.get("stance") or "neutral")
        by_type[evidence_type] = by_type.get(evidence_type, 0) + 1
        by_stance[stance] = by_stance.get(stance, 0) + 1
        confidence = evidence.get("confidence")
        if isinstance(confidence, int | float):
            confidence_values.append(float(confidence))
        raw_data = evidence.get("raw_data") or {}
        if isinstance(raw_data, dict) and raw_data.get("status") == "failed":
            failed_tools.append(str(evidence.get("source_tool") or raw_data.get("tool_name") or ""))

    return {
        "by_type": by_type,
        "by_stance": by_stance,
        "failed_tools": dedupe_strings([tool for tool in failed_tools if tool]),
        "average_evidence_confidence": _average(confidence_values),
        "supporting_count": by_stance.get("supporting", 0),
        "refuting_count": by_stance.get("refuting", 0),
        "neutral_count": by_stance.get("neutral", 0),
        "unknown_count": by_stance.get("unknown", 0),
    }


def _build_confidence_reasons(
    evidence_items: list[Any],
    conflicts: list[str],
    exhausted_failed_records: list[dict[str, Any]],
) -> list[str]:
    """Collect concise confidence explanations from evidence and analyzer signals."""
    reasons: list[str] = []
    for evidence in evidence_items:
        if not isinstance(evidence, dict):
            continue
        reason = str(evidence.get("confidence_reason") or "").strip()
        if reason:
            reasons.append(f"{evidence.get('source_tool', 'unknown')}: {reason}")

    reasons.extend(f"证据冲突降低置信度: {item}" for item in conflicts)
    reasons.extend(
        f"工具失败降级: {record.get('tool_name', 'unknown')} {record.get('error_message') or ''}".strip()
        for record in exhausted_failed_records
    )
    sources = _evidence_data_sources(evidence_items)
    if "mock" in sources:
        reasons.append("Mock 回退证据仅适合演示，降低诊断置信度")
    fallback_sources = sources.intersection(
        {"rule_based", "manual_analysis", "llm_toolnode_fallback"}
    )
    if fallback_sources:
        reasons.append("规则或人工/LLM 兜底证据需要真实工具复核")
    return dedupe_strings(reasons)


def _evidence_data_sources(evidence_items: list[Any]) -> set[str]:
    sources: set[str] = set()
    for evidence in evidence_items:
        if not isinstance(evidence, dict):
            continue
        data_source = str(evidence.get("data_source") or "").strip()
        if data_source and data_source != "unknown":
            sources.add(data_source)
        raw_data = evidence.get("raw_data") or {}
        if not isinstance(raw_data, dict):
            continue
        raw_source = str(raw_data.get("source") or "").strip()
        if raw_source:
            sources.add(raw_source)
        output = raw_data.get("output")
        if isinstance(output, dict):
            output_source = str(output.get("source") or "").strip()
            if output_source:
                sources.add(output_source)
        metadata = raw_data.get("metadata")
        if isinstance(metadata, dict):
            execution_path = str(metadata.get("execution_path") or "").strip()
            if execution_path:
                sources.add(execution_path)
    return sources


def _detect_conflicts(
    evidence_items: list[Any],
    input_text: str,
    incident: Any,
) -> list[str]:
    """Detect common cross-tool conflicts in diagnosis evidence."""
    context = " ".join(
        [
            input_text,
            str(incident.get("symptom", "")) if isinstance(incident, dict) else "",
            " ".join(
                str(item.get("summary", "")) for item in evidence_items if isinstance(item, dict)
            ),
        ]
    ).lower()
    conflicts: list[str] = []

    if _metrics_abnormal(evidence_items) and _logs_refuting(evidence_items):
        conflicts.append("指标异常但日志未发现对应 ERROR/timeout 证据")
    if _log_points_to_redis(evidence_items, context) and _redis_refuting(evidence_items):
        conflicts.append("日志指向 Redis timeout，但 Redis connected_clients/maxclients 状态正常")
    if has_k8s_oom_signal(evidence_items, context) and has_mysql_signal(evidence_items, context):
        conflicts.append("K8s OOM 与 MySQL 慢查询同时出现，需要人工确认主次根因")

    return dedupe_strings(conflicts)


def _metrics_abnormal(evidence_items: list[Any]) -> bool:
    return any(
        _type_from_tool(item) == "metric" and _evidence_supporting(item) for item in evidence_items
    )


def _logs_refuting(evidence_items: list[Any]) -> bool:
    return any(
        _type_from_tool(item) == "log" and _evidence_refuting(item) for item in evidence_items
    )


def _log_points_to_redis(evidence_items: list[Any], context: str) -> bool:
    if "redis" in context and "timeout" in context:
        return True
    return any(
        _type_from_tool(item) == "log"
        and mentions_any(str(evidence_output(item)).lower(), ["redis", "timeout"])
        for item in evidence_items
    )


def _redis_refuting(evidence_items: list[Any]) -> bool:
    return any(
        _type_from_tool(item) == "redis" and _evidence_refuting(item) for item in evidence_items
    )


def _evidence_supporting(evidence: Any) -> bool:
    return isinstance(evidence, dict) and str(evidence.get("stance") or "").lower() == "supporting"


def _evidence_refuting(evidence: Any) -> bool:
    return isinstance(evidence, dict) and str(evidence.get("stance") or "").lower() == "refuting"


def _type_from_tool(evidence: Any) -> str:
    if not isinstance(evidence, dict):
        return "unknown"
    evidence_type = str(evidence.get("evidence_type") or "")
    if evidence_type:
        return evidence_type
    tool_name = str(evidence.get("source_tool") or "")
    return infer_evidence_type(tool_name)


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)


def _bounded_confidence(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 2)
