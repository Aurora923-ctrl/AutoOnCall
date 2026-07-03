"""Markdown rendering for deterministic diagnosis reports."""

from __future__ import annotations

from typing import Any

from app.models.report import DiagnosisReport
from app.services.change_execution_read_models import build_change_execution_stages


def render_markdown(report: DiagnosisReport) -> str:
    """Render a diagnosis report into auditable Markdown."""
    risk_level = report.risk_summary.get("risk_level", "low")
    risk_policy = report.risk_summary.get("policy", "allow")
    approval_line = (
        "当前状态：等待人工审批。"
        if report.approval_status == "pending"
        else f"审批状态：{report.approval_status}。"
    )
    return "\n".join(
        [
            f"# {report.title}",
            "",
            "## 摘要",
            report.summary or "暂无摘要。",
            "",
            "## 根因判断",
            report.root_cause or "暂未形成明确根因。",
            "",
            "## 根因假设矩阵",
            _render_hypothesis_ranking(report),
            "",
            "## 已确认事实",
            _render_bullets(report.confirmed_facts),
            "",
            "## 推断结论",
            _render_bullets(report.inferred_conclusions),
            "",
            "## 影响范围",
            report.impact or "暂无影响范围信息。",
            "",
            "## 关键证据",
            _render_bullets(report.key_findings),
            "",
            "## 证据质量",
            _render_evidence_quality(report),
            "",
            "## 不确定性",
            _render_bullets(report.uncertainties) if report.uncertainties else "- 暂无",
            "",
            "## 运行告警",
            _render_bullets(report.warnings) if report.warnings else "- 暂无",
            "",
            "## 下一步建议",
            _render_bullets(report.next_steps),
            "",
            "## Runbook 引用",
            _render_runbook_references(report.evidence),
            "",
            "## 工具调用摘要",
            _render_tool_calls(report.tool_calls),
            "",
            "## Tracing 与消息队列证据",
            _render_dependency_signals(report.dependency_signals),
            "",
            "## 风险与审批",
            f"- 风险等级：{risk_level}",
            f"- 策略：{risk_policy}",
            f"- {approval_line}",
            _render_approval_decision(report),
            f"- 是否需要人工动作：{'是' if report.manual_action_required else '否'}",
            "",
            "## 变更计划草案",
            _render_change_plan(report),
            "",
            "## 安全变更执行",
            _render_change_executions(report),
            "",
            "## Trace 摘要",
            f"- trace_id：{report.trace_id or 'unknown'}",
            f"- 事件数：{report.trace_summary.get('event_count', 0)}",
            f"- 异常或阻断事件数：{report.trace_summary.get('failed_or_blocked_count', 0)}",
            "",
            "## 处理建议",
            report.remediation_suggestion or "暂无处理建议。",
            "",
            "## 人工动作与回滚边界",
            _render_manual_action_boundary(report),
            "",
            "## 预防建议",
            report.prevention or "暂无预防建议。",
            "",
            f"> 置信度原因：{report.confidence_reason}",
            f"> 报告置信度：{report.confidence:.2f}",
        ]
    )


def _render_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 无"


def _render_hypothesis_ranking(report: DiagnosisReport) -> str:
    if not report.hypothesis_ranking:
        return "- 暂无根因假设排序"

    lines: list[str] = []
    for index, item in enumerate(report.hypothesis_ranking[:6], 1):
        selected = "（选中）" if item.get("hypothesis_id") == report.selected_root_cause_id else ""
        lines.extend(
            [
                f"{index}. {item.get('title') or item.get('description') or '未命名假设'}{selected}",
                f"   - 分类：{item.get('category', 'unknown')}；置信度：{float(item.get('confidence') or 0.0):.2f}",
                f"   - 支持证据：{_render_inline_list(item.get('supporting_evidence_ids'))}",
                f"   - 反驳证据：{_render_inline_list(item.get('refuting_evidence_ids'))}",
                f"   - 缺失证据：{_render_inline_list(item.get('missing_evidence'))}",
                f"   - 置信度原因：{item.get('confidence_reason') or '未说明'}",
            ]
        )
    return "\n".join(lines)


def _render_change_plan(report: DiagnosisReport) -> str:
    plan = report.change_plan or {}
    if not plan:
        return "- 无待审批变更计划；如需生产写操作，必须另行生成审批和变更计划。"

    return "\n".join(
        [
            f"- 计划ID：{plan.get('change_plan_id') or '未记录'}",
            f"- 状态：{plan.get('status') or 'draft'}",
            f"- 动作：{plan.get('action') or '未记录'}",
            f"- 风险等级：{plan.get('risk_level') or 'medium'}",
            "- 前置检查：",
            _render_indented_bullets(plan.get("pre_checklist")),
            "- 人工执行步骤：",
            _render_indented_bullets(plan.get("execution_steps")),
            "- 回滚步骤：",
            _render_indented_bullets(plan.get("rollback_steps")),
            "- 验证步骤：",
            _render_indented_bullets(plan.get("verification_steps")),
            "- 边界：Agent 只生成建议和计划；生产写操作需在审批通过后进入安全变更流程。",
        ]
    )


def _render_change_executions(report: DiagnosisReport) -> str:
    executions = [item for item in report.change_executions if isinstance(item, dict)]
    if not executions:
        return "- 暂无安全变更执行记录。"

    lines: list[str] = []
    for item in executions[-5:]:
        raw_stages = item.get("stages")
        stages = raw_stages if isinstance(raw_stages, list) else build_change_execution_stages(item)
        lines.extend(
            [
                f"- 执行ID：{item.get('change_execution_id') or '未记录'}",
                f"  - 状态：{item.get('status') or 'unknown'}；模式：{item.get('mode') or 'unknown'}",
                f"  - 审批ID：{item.get('approval_id') or '未记录'}；计划ID：{item.get('change_plan_id') or '未记录'}",
            ]
        )
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            label = stage.get("label") or stage.get("key") or "stage"
            status = stage.get("status") or "未执行"
            reason = stage.get("reason") or "未记录"
            lines.append(f"  - {label}：{status}；{reason}")
    return "\n".join(lines)


def _render_inline_list(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "无"
    return ", ".join(str(item) for item in value)


def _render_indented_bullets(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "  - 无"
    return "\n".join(f"  - {item}" for item in value)


def _render_tool_calls(tool_calls: list[dict[str, Any]]) -> str:
    if not tool_calls:
        return "- 无"
    lines = []
    for call in tool_calls:
        lines.append(
            "- "
            f"{call.get('tool_name', 'unknown')} "
            f"step={call.get('step_id', '')} "
            f"source={call.get('data_source', 'unknown')} "
            f"status={call.get('status', 'unknown')} "
            f"latency_ms={call.get('latency_ms', 0)} "
            f"input={call.get('input_summary') or '未记录'} "
            f"summary={call.get('output_summary') or call.get('error_message') or '未记录'}"
        )
    return "\n".join(lines)


def _render_dependency_signals(signals: list[dict[str, Any]]) -> str:
    if not signals:
        return "- 无"
    lines: list[str] = []
    for item in signals:
        lines.append(
            "- "
            f"{item.get('domain', 'dependency')} "
            f"backend={item.get('backend', 'unknown')} "
            f"tool={item.get('tool_name', 'unknown')} "
            f"step={item.get('step_id', '')} "
            f"source={item.get('data_source', 'unknown')} "
            f"status={item.get('status', 'unknown')} "
            f"stance={item.get('stance', 'neutral')} "
            f"confidence={float(item.get('confidence') or 0.0):.2f} "
            f"summary={item.get('summary') or '未记录'}"
        )
    return "\n".join(lines)


def _render_approval_decision(report: DiagnosisReport) -> str:
    approval = report.approval_decision or report.risk_summary.get("approval_decision") or {}
    if not approval:
        return "- 审批详情：无"

    lines = [
        f"- 审批动作：{approval.get('action') or '未记录'}",
        f"- 审批ID：{approval.get('approval_id') or '未记录'}",
        f"- 审批人：{approval.get('decided_by') or '未处理'}",
        f"- 审批结果：{approval.get('status') or report.approval_status}",
        f"- 审批时间：{approval.get('decided_at') or '未处理'}",
        f"- 审批原因：{approval.get('decision_reason') or approval.get('reason') or '未填写'}",
    ]
    if approval.get("created_at"):
        lines.append(f"- 提交审批时间：{approval.get('created_at')}")
    if approval.get("tool_name"):
        lines.append(f"- 关联工具：{approval.get('tool_name')}")
    return "\n".join(lines)


def _render_evidence_quality(report: DiagnosisReport) -> str:
    profile = report.evidence_profile or {}
    by_type = _as_dict(profile.get("by_type"))
    by_stance = _as_dict(profile.get("by_stance"))
    lines = [
        f"- 类型分布：{_render_counter(by_type)}",
        f"- 立场分布：{_render_counter(by_stance)}",
    ]
    for item in report.evidence[:8]:
        lines.append(
            "- "
            f"{item.get('source_tool', 'unknown')} "
            f"source={item.get('data_source', 'unknown')} "
            f"type={item.get('evidence_type', 'unknown')} "
            f"stance={item.get('stance', 'neutral')} "
            f"confidence={float(item.get('confidence', 0.0)):.2f} "
            f"reason={item.get('confidence_reason', '') or '未标注'}"
        )
    return "\n".join(lines)


def _render_manual_action_boundary(report: DiagnosisReport) -> str:
    if report.manual_action_required:
        return "\n".join(
            [
                "- Agent 只输出诊断和处置建议；不直接执行生产写操作。",
                "- 人工执行前需要确认审批、影响范围、观察窗口和回滚方案。",
                "- 若变更后指标或日志未恢复，应立即回滚并升级人工排查。",
            ]
        )
    return "\n".join(
        [
            "- 当前报告未要求自动变更。",
            "- 如需执行重启、扩容、SQL 或配置修改，必须重新进入审批或变更流程。",
        ]
    )


def _render_counter(counter: dict[str, Any]) -> str:
    if not counter:
        return "无"
    return ", ".join(f"{key}={value}" for key, value in sorted(counter.items()))


def _render_runbook_references(evidence: list[dict[str, Any]]) -> str:
    references = _extract_runbook_references(evidence)
    if not references:
        return "- 无"

    lines = []
    for item in references[:8]:
        score = item.get("score")
        score_text = "未知" if score is None else f"{float(score):.4f}"
        heading = str(item.get("heading_path") or "未标注章节")
        lines.append(
            "- "
            f"{item.get('source_file', '未知来源')} "
            f"chunk={item.get('chunk_id', 'unknown')} "
            f"score={score_text} "
            f"heading={heading}"
        )
    return "\n".join(lines)


def _extract_runbook_references(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in evidence:
        payloads = _candidate_retrieval_payloads(item)
        for payload in payloads:
            for result in payload.get("retrieval_results", []) or []:
                if not isinstance(result, dict):
                    continue
                key = str(result.get("chunk_id") or result.get("source_file") or result)
                if key in seen:
                    continue
                seen.add(key)
                references.append(result)
    return references


def _candidate_retrieval_payloads(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    raw_data = _as_dict(evidence.get("raw_data"))
    output = _as_dict(raw_data.get("output"))
    payloads = []
    if raw_data.get("retrieval_results"):
        payloads.append(raw_data)
    if output.get("retrieval_results"):
        payloads.append(output)
    return payloads


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
