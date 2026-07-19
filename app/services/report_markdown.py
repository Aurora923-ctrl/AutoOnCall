"""Markdown rendering for deterministic diagnosis reports."""

from __future__ import annotations

import re
from typing import Any

from app.models.report import DiagnosisReport
from app.services.change_execution_read_models import build_change_execution_stages
from app.services.evidence_graph import evidence_layer as _shared_evidence_layer
from app.utils.structured_data import as_dict as _as_dict


def render_markdown(report: DiagnosisReport) -> str:
    """Render a diagnosis report into auditable Markdown.

    The first nine sections are the operator-facing incident review draft.
    Detailed tool, trace, and evidence internals are kept in appendices so the
    report can be pasted into a real OnCall postmortem without feeling like a
    debug dump.
    """
    risk_level = report.risk_summary.get("risk_level", "low")
    risk_policy = report.risk_summary.get("policy", "allow")
    approval_line = (
        "当前状态：等待人工审批。"
        if report.approval_status == "pending"
        else f"审批状态：{report.approval_status}。"
    )
    sections = [
        f"# {report.title}",
        "",
        "## 1. 故障摘要",
        _render_incident_summary(report),
        "",
        "## 2. 用户影响",
        report.impact or "暂无用户影响信息。",
        "",
        "## 3. 初步根因",
        _render_root_cause_with_evidence(report),
        "",
        "## 4. 关键证据",
        _render_key_evidence_for_review(report),
        "",
        "## 5. 排查过程",
        _render_investigation_process(report),
        "",
        "## 6. 风险动作判断",
        _render_risk_action_judgement(report),
        "",
        "## 7. 建议处置",
        _render_recommendation(report),
        "",
        "## 8. 回滚与观察指标",
        _render_rollback_and_observation(report),
        "",
        "## 9. 未确认事项",
        _render_unconfirmed_items(report),
        "",
        "## 附录 A. Evidence、Graph 与 Citation",
        "### 根因假设矩阵",
        _render_hypothesis_ranking(report),
        "",
        "### 已确认事实",
        _render_bullets(report.confirmed_facts),
        "",
        "### 推断结论",
        _render_bullets(report.inferred_conclusions),
        "",
        "### 证据质量",
        _render_evidence_quality(report),
        "",
        "### 证据充分性",
        _render_evidence_sufficiency(report),
        "",
        "### Conclusion Alignment",
        _render_conclusion_alignment(report),
        "",
        "### Incident Evidence Graph",
        _render_incident_evidence_graph(report),
        "",
        "### Citation / Runbook 引用",
        _render_runbook_references(report.evidence),
        "",
        "### 数据源边界",
        _render_data_source_boundaries(report),
        "",
        "### 诊断链路证据",
        _render_diagnostic_chains(report),
        "",
        "### 证据矩阵",
        _render_evidence_matrix(report),
        "",
        "### 不确定性",
        _render_bullets(report.uncertainties) if report.uncertainties else "- 暂无",
        "",
        "### 运行告警",
        _render_bullets(report.warnings) if report.warnings else "- 暂无",
        "",
        "## 附录 B. ToolCall 与 Trace",
        "### ToolCall 摘要",
        _render_tool_calls(report.tool_calls),
        "",
        "### Trace 摘要",
        f"- trace_id：{report.trace_id or 'unknown'}",
        f"- 事件数：{report.trace_summary.get('event_count', 0)}",
        f"- 异常或阻断事件数：{report.trace_summary.get('failed_or_blocked_count', 0)}",
        "",
        "## 附录 C. 风险、审批与变更",
        "### 风险与审批",
        f"- 风险等级：{risk_level}",
        f"- 策略：{risk_policy}",
        f"- {approval_line}",
        _render_approval_decision(report),
        f"- 是否需要人工动作：{'是' if report.manual_action_required else '否'}",
        "",
        "### 变更计划草案",
        _render_change_plan(report),
        "",
        "### 安全变更执行",
        _render_change_executions(report),
        "",
        "### 处理建议",
        report.remediation_suggestion or "暂无处理建议。",
        "",
        "### 人工动作与回滚边界",
        _render_manual_action_boundary(report),
        "",
        "### 预防建议",
        report.prevention or "暂无预防建议。",
    ]
    if report.dependency_signals:
        trace_summary_index = sections.index("### Trace 摘要")
        sections[trace_summary_index:trace_summary_index] = [
            "### Tracing 与消息队列证据",
            _render_dependency_signals(report.dependency_signals),
            "",
        ]
    return _neutralize_active_markdown("\n".join(sections))


def _neutralize_active_markdown(markdown: str) -> str:
    """Prevent report data from becoming active HTML or script-scheme links."""
    escaped = markdown.replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(
        r"(?i)\b(javascript|vbscript|data)\s*:",
        lambda match: f"{match.group(1)}&#58;",
        escaped,
    )


def _render_incident_summary(report: DiagnosisReport) -> str:
    """Keep service, impact, root cause, and confidence visible without scrolling."""
    return "\n".join(
        [
            "| 服务 | 环境 | 级别 | 状态 | 置信度 |",
            "| --- | --- | --- | --- | ---: |",
            f"| {_md_cell(report.service_name)} | {_md_cell(report.environment)} | "
            f"{_md_cell(report.severity)} | {_md_cell(report.status)} | {report.confidence:.2f} |",
            "",
            f"- 摘要：{_limit_sentences(report.summary or '暂无摘要。', maximum=2)}",
            f"- 用户影响：{_limit_sentences(report.impact or '暂无用户影响信息。', maximum=1)}",
            f"- 初步根因：{_limit_sentences(report.root_cause or '尚未形成明确根因。', maximum=3)}",
            f"- 置信度依据：{_limit_sentences(report.confidence_reason or '未记录', maximum=1)}",
        ]
    )


def _render_key_evidence_for_review(report: DiagnosisReport) -> str:
    evidence = _select_review_evidence(report)
    if not evidence:
        return "- 无关键证据。"
    lines = [
        "| Evidence | Tool / Source | Fact | Inference | Uncertainty |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in evidence:
        lines.append(
            "| "
            f"{_md_cell(item.get('evidence_id', 'unknown'))} | "
            f"{_md_cell(item.get('source_tool', 'unknown'))} / "
            f"{_md_cell(item.get('data_source', 'unknown'))} | "
            f"{_md_cell(item.get('fact') or item.get('summary') or '')} | "
            f"{_md_cell(item.get('inference') or '')} | "
            f"{_md_cell(item.get('uncertainty') or '')} |"
        )
    return "\n".join(lines)


def _render_investigation_process(report: DiagnosisReport) -> str:
    if not report.tool_calls:
        return "- 未记录工具调用。"
    lines = [
        "| # | Tool | Source | Status | Latency | Result |",
        "| ---: | --- | --- | --- | ---: | --- |",
    ]
    for index, call in enumerate(_select_review_tool_calls(report.tool_calls), 1):
        status = str(call.get("status") or "unknown")
        result = call.get("error_message") if status == "failed" else call.get("output_summary")
        lines.append(
            f"| {index} | {_md_cell(call.get('tool_name', 'unknown'))} | "
            f"{_md_cell(call.get('data_source', 'unknown'))} | {_md_cell(status)} | "
            f"{call.get('latency_ms', 0)} ms | {_md_cell(result or '未记录')} |"
        )
    return "\n".join(lines)


def _render_risk_action_judgement(report: DiagnosisReport) -> str:
    risk_level = report.risk_summary.get("risk_level", "low")
    risk_policy = report.risk_summary.get("policy", "allow")
    approval = report.approval_status or "not_required"
    lines = [
        f"- 风险等级：{risk_level}",
        f"- 风险策略：{risk_policy}",
        f"- 审批状态：{approval}",
        f"- 是否需要人工动作：{'是' if report.manual_action_required else '否'}",
    ]
    if risk_policy == "forbidden":
        lines.append("- 结论：禁止自动执行，必须转人工变更流程。")
    elif report.manual_action_required or approval not in {"not_required", ""}:
        lines.append("- 结论：诊断可只读推进，生产写操作必须审批后执行。")
    else:
        lines.append("- 结论：当前诊断阶段不需要审批；后续如涉及生产写操作需重新审批。")
    return "\n".join(lines)


def _render_recommendation(report: DiagnosisReport) -> str:
    plan = report.change_plan or {}
    playbook = _as_dict(plan.get("remediation_playbook"))
    prechecks = _compact_list(playbook.get("pre_check") or plan.get("pre_checklist"), limit=2)
    dry_run = _compact_list(playbook.get("dry_run"), limit=2)
    observations = _compact_list(
        playbook.get("observe_metrics") or plan.get("observe_metrics"),
        limit=5,
    )
    rollback = _compact_list(playbook.get("rollback") or plan.get("rollback_steps"), limit=2)
    policy = report.risk_summary.get("policy")
    approval = (
        "禁止自动执行，必须转人工变更流程。"
        if policy == "forbidden"
        else (
            "生产写操作必须完成人工审批；诊断和证据采集保持只读。"
            if report.manual_action_required
            or report.risk_summary.get("need_approval")
            or policy == "approval_required"
            else "当前只读诊断无需审批；进入生产写操作前必须重新进行风险判断和审批。"
        )
    )
    return "\n".join(
        [
            _limit_sentences(report.remediation_suggestion or "暂无处理建议。", maximum=3),
            "",
            f"- 前置检查：{_inline_or_default(prechecks, '复核当前证据、影响面和变更窗口。')}",
            f"- 审批边界：{approval}",
            f"- Dry-run：{_inline_or_default(dry_run, '仅生成动作预览并校验目标、参数和影响范围，不执行生产写操作。')}",
            f"- 观察：{_inline_or_default(observations, '按第 8 节观察核心指标和错误日志。')}",
            f"- 回滚：{_inline_or_default(rollback, '指标未恢复或出现新告警时停止动作并恢复变更前状态。')}",
        ]
    )


def _render_rollback_and_observation(report: DiagnosisReport) -> str:
    plan = report.change_plan or {}
    playbook = _as_dict(plan.get("remediation_playbook"))
    metrics = _compact_list(
        playbook.get("observe_metrics") or plan.get("observe_metrics"),
        limit=6,
    )
    rollback_steps = _compact_list(
        playbook.get("rollback") or plan.get("rollback_steps"),
        limit=4,
    )
    stop_conditions = _compact_list(playbook.get("stop_conditions"), limit=3)
    lines = [
        "- 观察指标：",
        *[f"  - {item}" for item in (metrics or _default_observation_metrics(report))],
        "- 停止 / 回滚条件：",
        *[
            f"  - {item}"
            for item in (stop_conditions or ["核心指标未恢复、继续恶化或出现新的高严重级别告警。"])
        ],
        "- 回滚步骤：",
        *[
            f"  - {item}"
            for item in (
                rollback_steps or ["停止后续动作，恢复变更前状态，并重新采集指标、日志和依赖状态。"]
            )
        ],
    ]
    return "\n".join(lines)


def _render_unconfirmed_items(report: DiagnosisReport) -> str:
    items = list(report.uncertainties or [])
    sufficiency = report.evidence_sufficiency or _as_dict(
        (report.evidence_profile or {}).get("sufficiency")
    )
    missing = sufficiency.get("missing_evidence")
    if isinstance(missing, list):
        items.extend(f"缺失证据：{item}" for item in missing)
    failed = sufficiency.get("failed_tools")
    if isinstance(failed, list) and failed:
        items.append("失败工具：" + "、".join(str(item) for item in failed))
    cap = sufficiency.get("confidence_cap")
    if cap is not None:
        items.append(f"当前置信度上限：{float(cap):.2f}")
    return _render_bullets(_dedupe_inline([str(item) for item in items if str(item).strip()]))


def _select_review_evidence(report: DiagnosisReport) -> list[dict[str, Any]]:
    """Select at most five non-duplicated items for the operator-facing body."""
    root_ids = set(_selected_root_cause_evidence_ids(report))
    candidates = sorted(
        [item for item in report.evidence if isinstance(item, dict)],
        key=lambda item: (
            0 if str(item.get("evidence_id") or "") in root_ids else 1,
            0 if item.get("stance") == "supporting" else 1,
            -float(item.get("confidence") or 0.0),
        ),
    )
    selected: list[dict[str, Any]] = []
    seen_facts: set[str] = set()
    for item in candidates:
        fact = _normalized_evidence_text(item.get("fact") or item.get("summary") or "")
        if fact and fact in seen_facts:
            continue
        selected.append(item)
        if fact:
            seen_facts.add(fact)
        if len(selected) == 5:
            break
    return selected


def _select_review_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the main investigation readable while never hiding a failed call."""
    selected = list(tool_calls[:8])
    selected_ids = {
        (call.get("step_id"), call.get("tool_name"), call.get("status")) for call in selected
    }
    for call in tool_calls:
        identity = (call.get("step_id"), call.get("tool_name"), call.get("status"))
        if call.get("status") == "failed" and identity not in selected_ids:
            selected.append(call)
            selected_ids.add(identity)
    return selected


def _normalized_evidence_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").lower()).strip()
    return re.sub(r"\b\d+(?:\.\d+)?\b", "#", text)


def _default_observation_metrics(report: DiagnosisReport) -> list[str]:
    root = report.root_cause.lower()
    if "redis" in root:
        return [
            "redis_connected_clients / maxclients",
            "redis_rejected_connections",
            "service_5xx_rate",
            "service_p95_latency_ms",
            "redis_timeout_log_count",
        ]
    if "mysql" in root or "sql" in root:
        return [
            "mysql_slow_query_count",
            "mysql_threads_running / active_connections",
            "connection_pool_waiting",
            "service_p95_latency_ms",
            "service_5xx_rate",
        ]
    return ["service_p95_latency_ms", "service_5xx_rate", "error_log_count"]


def _compact_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return _dedupe_inline([str(item).strip() for item in value if str(item).strip()])[:limit]


def _inline_or_default(items: list[str], default: str) -> str:
    return "；".join(items) if items else default


def _limit_sentences(value: str, *, maximum: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    sentences = [item.strip() for item in re.split(r"(?<=[。！？.!?])\s*", text) if item.strip()]
    if not sentences:
        return text[:360]
    return " ".join(sentences[:maximum])[:480]


def _selected_root_cause_evidence_ids(report: DiagnosisReport) -> list[str]:
    for item in report.hypothesis_ranking:
        if item.get("hypothesis_id") == report.selected_root_cause_id:
            raw_ids = item.get("supporting_evidence_ids")
            if isinstance(raw_ids, list):
                return [str(value) for value in raw_ids if str(value).strip()]
    return [
        str(item.get("evidence_id"))
        for item in report.evidence
        if item.get("evidence_id") and item.get("stance") == "supporting"
    ][:5]


def _render_tool_call_table(tool_calls: list[dict[str, Any]]) -> str:
    """Render the most important tool-call fields as a Markdown table."""
    if not tool_calls:
        return "- No tool calls recorded."
    lines = [
        "| Tool | Source | Status | Latency ms | Artifact | Summary |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for call in tool_calls[:8]:
        lines.append(
            "| "
            f"{_md_cell(call.get('tool_name', 'unknown'))} | "
            f"{_md_cell(call.get('data_source', 'unknown'))} | "
            f"{_md_cell(call.get('status', 'unknown'))} | "
            f"{call.get('latency_ms', 0)} | "
            f"{_md_cell(_artifact_label(call.get('output_artifact')))} | "
            f"{_md_cell(call.get('output_summary') or call.get('error_message') or '')} |"
        )
    return "\n".join(lines)


def _render_evidence_quick_view(evidence: list[dict[str, Any]]) -> str:
    """Render supporting/refuting/unknown examples for quick report inspection."""
    rows = [
        ("supporting", _evidence_by_stance(evidence, "supporting")),
        ("refuting", _evidence_by_stance(evidence, "refuting")),
        ("unknown", _unknown_evidence(evidence)),
    ]
    lines = ["| Stance | Count | Example |", "| --- | ---: | --- |"]
    for stance, items in rows:
        example = _evidence_example(items[0]) if items else ""
        lines.append(f"| {stance} | {len(items)} | {_md_cell(example)} |")
    return "\n".join(lines)


def _evidence_example(item: dict[str, Any]) -> str:
    text = (
        str(item.get("fact") or "").strip()
        or str(item.get("summary") or "").strip()
        or str(item.get("uncertainty") or "").strip()
    )
    source = item.get("data_source", "unknown")
    tool = item.get("source_tool", "unknown")
    confidence = float(item.get("confidence") or 0.0)
    return f"{tool} source={source} confidence={confidence:.2f} {text}".strip()


def _md_cell(value: Any) -> str:
    """Keep Markdown table cells compact and valid."""
    text = str(value or "").replace("\n", " ").replace("|", "\\|").strip()
    return text[:180] + "..." if len(text) > 180 else text


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
            "- 结构化安全处置 Playbook：",
            _render_remediation_playbook(plan.get("remediation_playbook")),
            "- 边界：Agent 只生成建议和计划；生产写操作需在审批通过后进入安全变更流程。",
        ]
    )


def _render_remediation_playbook(value: Any) -> str:
    playbook = _as_dict(value)
    if not playbook:
        return "  - 未生成结构化 playbook。"
    lines = [
        f"  - 摘要：{playbook.get('summary') or '未记录'}",
        f"  - 风险策略：{playbook.get('risk_policy') or 'approval_required'}；"
        f"需要审批：{'是' if playbook.get('approval_required', True) else '否'}",
        "  - Pre-check：",
        *_nested_bullets(playbook.get("pre_check"), indent="    "),
        "  - Dry-run：",
        *_nested_bullets(playbook.get("dry_run"), indent="    "),
        "  - Sandbox / Manual record：",
        *_nested_bullets(playbook.get("sandbox_or_manual_record"), indent="    "),
        "  - Rollback：",
        *_nested_bullets(playbook.get("rollback"), indent="    "),
        "  - Observe metrics：",
        *_nested_bullets(playbook.get("observe_metrics"), indent="    "),
        "  - Stop conditions：",
        *_nested_bullets(playbook.get("stop_conditions"), indent="    "),
        "  - Safety notes：",
        *_nested_bullets(playbook.get("safety_notes"), indent="    "),
    ]
    return "\n".join(lines)


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


def _nested_bullets(value: Any, *, indent: str) -> list[str]:
    if not isinstance(value, list) or not value:
        return [f"{indent}- 无"]
    return [f"{indent}- {item}" for item in value]


def _render_tool_calls(tool_calls: list[dict[str, Any]]) -> str:
    if not tool_calls:
        return "- 无"
    lines = []
    for call in tool_calls:
        artifact = _artifact_label(call.get("output_artifact"))
        artifact_text = f"artifact={artifact} " if artifact else ""
        lines.append(
            "- "
            f"{call.get('tool_name', 'unknown')} "
            f"step={call.get('step_id', '')} "
            f"source={call.get('data_source', 'unknown')} "
            f"status={call.get('status', 'unknown')} "
            f"latency_ms={call.get('latency_ms', 0)} "
            f"{artifact_text}"
            f"input={call.get('input_summary') or '未记录'} "
            f"summary={call.get('output_summary') or call.get('error_message') or '未记录'}"
        )
    return "\n".join(lines)


def _artifact_label(value: Any) -> str:
    artifact = _as_dict(value)
    if not artifact:
        return ""
    artifact_id = artifact.get("artifact_id") or "artifact"
    size = artifact.get("size_bytes")
    if isinstance(size, int | float):
        return f"{artifact_id} ({int(size)} bytes)"
    return str(artifact_id)


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
    by_data_source = _as_dict(profile.get("by_data_source"))
    by_layer = _as_dict(profile.get("by_layer"))
    root_cause_closure = _as_dict(profile.get("root_cause_closure"))
    root_cause_reference_ids = _root_cause_reference_ids(root_cause_closure)
    failed_tools = profile.get("failed_tools")
    lines = [
        f"- 类型分布：{_render_counter(by_type)}",
        f"- 立场分布：{_render_counter(by_stance)}",
        f"- 数据源分布：{_render_counter(by_data_source)}",
        f"- 证据层级：{_render_counter(by_layer)}",
        f"- 根因闭环：{root_cause_closure.get('status') or 'unknown'}；"
        f"live={_render_inline_list(root_cause_closure.get('live_evidence_ids'))}；"
        f"knowledge/history={_render_inline_list(root_cause_reference_ids)}",
        f"- 大输出 Artifact 数：{profile.get('artifact_count', 0)}",
        f"- 失败工具：{_render_inline_list(failed_tools)}",
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


def _root_cause_reference_ids(root_cause_closure: dict[str, Any]) -> list[Any]:
    knowledge = root_cause_closure.get("knowledge_evidence_ids")
    history = root_cause_closure.get("history_evidence_ids")
    return [
        *(knowledge if isinstance(knowledge, list) else []),
        *(history if isinstance(history, list) else []),
    ]


def _render_evidence_sufficiency(report: DiagnosisReport) -> str:
    """Render the business gate that prevents overconfident reports."""
    sufficiency = report.evidence_sufficiency or _as_dict(
        (report.evidence_profile or {}).get("sufficiency")
    )
    if not sufficiency:
        return "- 未记录证据充分性判断。"

    status = str(sufficiency.get("status") or "unknown")
    complete = bool(sufficiency.get("complete"))
    missing = sufficiency.get("missing_evidence")
    failed = sufficiency.get("failed_tools")
    confidence_cap = sufficiency.get("confidence_cap")
    degradation = report.degradation_analysis or {}
    lines = [
        f"- 门槛状态：{status}；是否允许 completed：{'是' if complete else '否'}。",
        "- 主故障域工具证据："
        f"{'已满足' if sufficiency.get('has_primary_domain_evidence') else '缺失'}；"
        f"来源={_render_inline_list(sufficiency.get('primary_domain_tools'))}",
        "- 现象侧证据："
        f"{'已满足' if sufficiency.get('has_symptom_evidence') else '缺失'}；"
        f"来源={_render_inline_list(sufficiency.get('symptom_tools'))}",
        "- 处置参考："
        f"{'已满足' if sufficiency.get('has_reference_evidence') else '缺失'}；"
        f"来源={_render_inline_list(sufficiency.get('reference_tools'))}",
        f"- 缺失证据：{_render_inline_list(missing)}",
        f"- 失败工具：{_render_inline_list(failed)}",
    ]
    if degradation:
        lines.extend(
            [
                f"- 降级根因：{degradation.get('category') or 'unknown'}",
                "- 安全终态："
                f"{'是' if degradation.get('safe_terminal') else '否'}；"
                f"需人工接管：{'是' if degradation.get('needs_human') else '否'}",
            ]
        )
    if confidence_cap is not None:
        lines.append(f"- 当前置信度上限：{float(confidence_cap):.2f}")
    if complete:
        lines.append("- 结论边界：主故障域、现象侧和处置参考均已覆盖，报告可以保持 completed。")
    else:
        lines.append(
            "- 结论边界：证据未满足 completed 门槛，报告必须降级为 incomplete、degraded "
            "或 needs_human，不能输出过度确定的根因。"
        )
    return "\n".join(lines)


def _render_conclusion_alignment(report: DiagnosisReport) -> str:
    alignment = report.conclusion_alignment or {}
    if not alignment:
        return "- Not recorded."

    fields = _as_dict(alignment.get("fields"))
    lines = [
        f"- Status: {alignment.get('status', 'unknown')}",
        f"- Missing fields: {_render_inline_list(alignment.get('missing_fields'))}",
    ]

    root = _as_dict(fields.get("root_cause"))
    lines.append(
        "- root_cause: "
        f"aligned={str(bool(root.get('aligned'))).lower()}; "
        f"evidence={_render_inline_list(root.get('evidence_ids'))}; "
        f"citations={_render_alignment_citations(root.get('citations'))}"
    )

    key_findings = fields.get("key_findings")
    if isinstance(key_findings, list):
        for index, item in enumerate(key_findings[:8], 1):
            finding = _as_dict(item)
            lines.append(
                f"- key_findings[{index}]: "
                f"aligned={str(bool(finding.get('aligned'))).lower()}; "
                f"evidence={_render_inline_list(finding.get('evidence_ids'))}; "
                f"citations={_render_alignment_citations(finding.get('citations'))}; "
                f"text={_md_cell(finding.get('text', ''))}"
            )

    remediation = _as_dict(fields.get("remediation_suggestion"))
    lines.append(
        "- remediation_suggestion: "
        f"aligned={str(bool(remediation.get('aligned'))).lower()}; "
        f"evidence={_render_inline_list(remediation.get('evidence_ids'))}; "
        f"citations={_render_alignment_citations(remediation.get('citations'))}"
    )
    return "\n".join(lines)


def _render_alignment_citations(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "none"
    rendered = []
    for item in value[:5]:
        if not isinstance(item, dict):
            continue
        source_file = item.get("source_file")
        chunk_id = item.get("chunk_id")
        if source_file and chunk_id:
            rendered.append(f"{source_file}#{chunk_id}")
    return ", ".join(rendered) if rendered else "none"


def _render_data_source_boundaries(report: DiagnosisReport) -> str:
    """Explain replay evidence versus current runtime state when both are present."""
    lines: list[str] = []
    for item in report.evidence:
        output = _evidence_output(item)
        if not output:
            continue
        if output.get("source") == "redis_info" and output.get("incident_evidence"):
            incident_evidence = _as_dict(output.get("incident_evidence"))
            live_info = _as_dict(output.get("live_info"))
            incident_connected = incident_evidence.get("connected_clients", "unknown")
            incident_maxclients = incident_evidence.get("maxclients", "unknown")
            live_connected = live_info.get(
                "connected_clients", output.get("live_connected_clients", "unknown")
            )
            live_maxclients = live_info.get("maxclients", output.get("live_maxclients", "unknown"))
            lines.append(
                "- Redis：live_info 是当前容器运行态 "
                f"(connected_clients={live_connected}/maxclients={live_maxclients})；"
                "incident_evidence 是回放事故窗口证据 "
                f"(key={incident_evidence.get('_key', 'unknown')}, "
                f"connected_clients={incident_connected}/maxclients={incident_maxclients})。"
            )
            lines.append(
                "- Redis：根因判断使用 incident_evidence 还原事故窗口；live_info 用于证明"
                "当前容器连通和运行态，不声称当前 Redis 仍处于连接打满状态。"
            )
        if output.get("source") == "mysql" and output.get("incident_evidence"):
            incident_evidence = _as_dict(output.get("incident_evidence"))
            live_status = _as_dict(output.get("live_status"))
            slow_queries = live_status.get("Slow_queries", "unknown")
            threads_connected = live_status.get("Threads_connected", "unknown")
            observed = incident_evidence.get("observed_value") or incident_evidence.get(
                "summary",
                "unknown",
            )
            lines.append(
                "- MySQL：live_status 是当前容器运行态 "
                f"(Slow_queries={slow_queries}, Threads_connected={threads_connected})；"
                f"incident_evidence 是事故窗口证据 ({observed})。"
            )
            lines.append(
                "- MySQL：根因判断使用 incident_evidence / payment_events 还原慢 SQL、"
                "active connections 和 pool waiting；live_status 用于证明当前 MySQL "
                "适配器连通和运行态，不声称当前 Slow_queries runtime counter 仍在增长。"
            )
    if lines:
        return "\n".join(_dedupe_inline(lines))
    return "- 当前报告未发现需要额外说明的 replay/runtime 边界；各证据默认按工具调用时间窗口解释。"


def _render_diagnostic_chains(report: DiagnosisReport) -> str:
    lines: list[str] = []
    for item in report.evidence:
        output = _evidence_output(item)
        redis_timeline = output.get("evidence_timeline")
        mysql_chain = output.get("evidence_chain")
        if isinstance(redis_timeline, list) and redis_timeline:
            lines.append("### Redis Evidence Timeline")
            lines.extend(_render_chain_items(redis_timeline))
        if isinstance(mysql_chain, list) and mysql_chain:
            lines.append("### MySQL Evidence Chain")
            lines.extend(_render_chain_items(mysql_chain))
    if lines:
        return "\n".join(lines)
    return "- 暂无结构化诊断链路证据。"


def _render_chain_items(items: list[Any]) -> list[str]:
    lines: list[str] = []
    for raw_item in items[:8]:
        item = _as_dict(raw_item)
        if not item:
            continue
        lines.append(
            "- "
            f"stage={_md_cell(item.get('stage', 'unknown'))}; "
            f"fact={_md_cell(item.get('fact', ''))}; "
            f"inference={_md_cell(item.get('inference', ''))}; "
            f"uncertainty={_md_cell(item.get('uncertainty', ''))}"
        )
    return lines or ["- 无"]


def _render_evidence_matrix_legacy(report: DiagnosisReport) -> str:
    groups = {
        "支持证据": _evidence_by_stance(report.evidence, "supporting"),
        "反驳证据": _evidence_by_stance(report.evidence, "refuting"),
        "不确定证据": _unknown_evidence(report.evidence),
        "中性上下文": _evidence_by_stance(report.evidence, "neutral"),
    }
    lines: list[str] = []
    for title, items in groups.items():
        lines.append(f"### {title}")
        if not items:
            lines.append("- 无")
            continue
        for item in items[:8]:
            lines.append(_render_evidence_matrix_item(item))
    return "\n".join(lines)


def _evidence_by_stance(evidence: list[dict[str, Any]], stance: str) -> list[dict[str, Any]]:
    return [item for item in evidence if str(item.get("stance") or "neutral") == stance]


def _unknown_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in evidence:
        raw_data = _as_dict(item.get("raw_data"))
        stance = str(item.get("stance") or "neutral")
        if stance == "unknown" or raw_data.get("status") == "failed":
            items.append(item)
    return items


def _render_evidence_matrix_item_legacy(item: dict[str, Any]) -> str:
    raw_data = _as_dict(item.get("raw_data"))
    status = raw_data.get("status") or "unknown"
    summary = (
        str(item.get("fact") or "").strip()
        or str(item.get("summary") or "").strip()
        or str(item.get("uncertainty") or "").strip()
        or "无摘要"
    )
    return (
        "- "
        f"id={item.get('evidence_id', 'unknown')} "
        f"tool={item.get('source_tool', 'unknown')} "
        f"source={item.get('data_source', 'unknown')} "
        f"type={item.get('evidence_type', 'unknown')} "
        f"status={status} "
        f"confidence={float(item.get('confidence') or 0.0):.2f} "
        f"summary={summary}"
    )


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


def _evidence_output(evidence: dict[str, Any]) -> dict[str, Any]:
    raw_data = _as_dict(evidence.get("raw_data"))
    output = _as_dict(raw_data.get("output"))
    if output:
        return output
    return raw_data


def _dedupe_inline(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


# Interview-oriented evidence rendering. These definitions intentionally live
# after the legacy helpers so the report keeps the old sections while upgrading
# the matrix that interviewers inspect first.
LIVE_EVIDENCE_SOURCES = {
    "redis_info",
    "mysql",
    "prometheus",
    "loki",
}
LIVE_EVIDENCE_TOOLS = {
    "query_redis_status",
    "query_mysql_status",
    "query_metrics",
    "query_logs",
}
LIVE_EVIDENCE_TYPES = {
    "redis",
    "mysql",
    "metric",
    "log",
}
KNOWLEDGE_EVIDENCE_TOOLS = {
    "search_runbook",
    "retrieve_runbook",
    "retrieve_knowledge",
}
KNOWLEDGE_EVIDENCE_TYPES = {
    "runbook",
    "knowledge",
}
KNOWLEDGE_DOC_SUFFIXES = (
    ".md",
    ".markdown",
    ".pdf",
    ".html",
    ".htm",
)
HISTORY_EVIDENCE_SOURCES = {
    "ticket_api",
    "deploy_history",
}
HISTORY_EVIDENCE_TOOLS = {
    "search_history_ticket",
    "query_deploy_history",
}
HISTORY_EVIDENCE_TYPES = {
    "ticket",
    "deploy_history",
}
HISTORY_DOC_SUFFIXES = (
    ".csv",
    ".xlsx",
)


def _render_root_cause_with_evidence(report: DiagnosisReport) -> str:
    root = _limit_sentences(report.root_cause or "暂未形成明确根因。", maximum=3)
    evidence_ids = _selected_root_cause_evidence_ids(report)[:5]
    lines = [
        root,
        "",
        f"- 置信度：{report.confidence:.2f}",
        f"- 判断依据：{_limit_sentences(report.confidence_reason or '未记录', maximum=1)}",
    ]
    if evidence_ids:
        lines.append(f"- Evidence back-links: {', '.join(evidence_ids)}")
    else:
        lines.append(
            "- Evidence back-links: no stable evidence_id recorded; review key evidence below."
        )
    return "\n".join(lines)


def _root_cause_minimum_evidence_links(
    report: DiagnosisReport, evidence_ids: list[str]
) -> list[str]:
    evidence_id_set = set(evidence_ids)
    linked = [
        item
        for item in report.evidence
        if not evidence_id_set or str(item.get("evidence_id") or "") in evidence_id_set
    ]
    if not linked:
        linked = list(report.evidence)

    live = _first_layer_evidence(linked, "live") or _first_layer_evidence(report.evidence, "live")
    knowledge = _first_layer_evidence(linked, "knowledge") or _first_layer_evidence(
        report.evidence, "knowledge"
    )
    history = _first_layer_evidence(linked, "history") or _first_layer_evidence(
        report.evidence, "history"
    )
    reference = knowledge or history
    reference_kind = "knowledge" if knowledge else "history"

    lines = ["- Root-cause evidence closure:"]
    if live:
        lines.append(f"  - live evidence: {_evidence_short_ref(live)}")
    else:
        lines.append("  - live evidence: missing")
    if reference:
        lines.append(f"  - {reference_kind} basis: {_evidence_short_ref(reference)}")
    else:
        lines.append("  - knowledge/history basis: missing")
    if live and reference:
        lines.append("  - closure: satisfied (live + knowledge/history)")
    else:
        lines.append("  - closure: incomplete; root-cause claim needs more backing")
    return lines


def _render_evidence_matrix(report: DiagnosisReport) -> str:
    groups = [
        (
            "Live Evidence",
            "current adapter or incident-window facts used to prove symptoms and runtime state",
            _evidence_by_interview_layer(report.evidence, "live"),
        ),
        (
            "Knowledge Basis",
            "Runbook, postmortem, or wiki material used to explain why the symptom implies this RCA",
            _evidence_by_interview_layer(report.evidence, "knowledge"),
        ),
        (
            "Historical Experience",
            "tickets, deploy history, or tables used to compare similar incidents and changes",
            _evidence_by_interview_layer(report.evidence, "history"),
        ),
        (
            "Other / Uncertain Evidence",
            "neutral, failed, or incomplete evidence that should not be overstated",
            _uncategorized_evidence(report.evidence),
        ),
    ]
    closure = _as_dict(_as_dict(report.evidence_graph).get("root_cause_closure"))
    root_support_ids = {str(item) for item in closure.get("supporting_evidence_ids") or []}
    lines = [
        "- Matrix rule: every root-cause conclusion should link to at least "
        "one live evidence item plus one knowledge or historical basis.",
        f"- Root-cause closure status: {closure.get('status', 'unknown')}",
    ]
    for title, purpose, items in groups:
        lines.append(f"### {title}")
        lines.append(f"- Layer role: {purpose}.")
        if not items:
            lines.append("- none")
            continue
        for item in items[:8]:
            lines.append(_render_evidence_matrix_item(item, root_support_ids=root_support_ids))
    return "\n".join(lines)


def _render_incident_evidence_graph(report: DiagnosisReport) -> str:
    graph = _as_dict(report.evidence_graph)
    if not graph:
        return "- Graph not recorded."

    stats = _as_dict(graph.get("stats"))
    closure = _as_dict(graph.get("root_cause_closure"))
    nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
    edges = [item for item in graph.get("edges", []) if isinstance(item, dict)]
    lines = [
        f"- graph_id: {graph.get('graph_id', 'unknown')}",
        f"- nodes: {stats.get('node_count', len(nodes))}; edges: {stats.get('edge_count', len(edges))}",
        f"- root-cause closure: {closure.get('status', 'unknown')}",
        f"- required layers: {_render_inline_list(closure.get('required_layers'))}",
        f"- live evidence: {_render_inline_list(closure.get('live_evidence_ids'))}",
        f"- knowledge evidence: {_render_inline_list(closure.get('knowledge_evidence_ids'))}",
        f"- history evidence: {_render_inline_list(closure.get('history_evidence_ids'))}",
    ]
    if closure.get("missing_layers"):
        lines.append(f"- missing layers: {_render_inline_list(closure.get('missing_layers'))}")

    lines.append("#### Root Cause Support Nodes")
    support_nodes = [
        node
        for node in nodes
        if node.get("node_type") == "evidence" and node.get("is_root_cause_support")
    ]
    if support_nodes:
        for node in support_nodes[:8]:
            lines.append(
                "- "
                f"{node.get('evidence_id', 'unknown')} "
                f"layer={node.get('layer', 'other')} "
                f"stance={node.get('stance', 'neutral')} "
                f"source={node.get('data_source', 'unknown')} "
                f"confidence={float(node.get('confidence') or 0.0):.2f} "
                f"label={_md_cell(node.get('label', ''))}"
            )
    else:
        lines.append("- none")

    lines.append("#### Key Edges")
    key_edges = [
        edge
        for edge in edges
        if edge.get("relation")
        in {"has_hypothesis", "supported_by", "refuted_by", "grounded_in", "produced_by"}
    ]
    if key_edges:
        for edge in key_edges[:12]:
            lines.append(
                "- "
                f"{edge.get('source', 'unknown')} "
                f"-[{edge.get('relation', 'rel')}]-> "
                f"{edge.get('target', 'unknown')}"
            )
    else:
        lines.append("- none")
    return "\n".join(lines)


def _evidence_by_interview_layer(
    evidence: list[dict[str, Any]], layer: str
) -> list[dict[str, Any]]:
    return [item for item in evidence if _evidence_layer(item) == layer]


def _uncategorized_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in evidence if _evidence_layer(item) == "other"]


def _first_layer_evidence(evidence: list[dict[str, Any]], layer: str) -> dict[str, Any] | None:
    for item in evidence:
        if _evidence_layer(item) == layer and str(item.get("stance") or "") == "supporting":
            return item
    for item in evidence:
        if _evidence_layer(item) == layer:
            return item
    return None


def _evidence_layer(item: dict[str, Any]) -> str:
    return _shared_evidence_layer(item)


def _evidence_source_files(item: dict[str, Any]) -> list[str]:
    files: list[str] = []
    output = _evidence_output(item)
    for key in ("source_file", "source_path", "file_name", "path"):
        value = output.get(key)
        if value:
            files.append(str(value))
    for payload in _candidate_retrieval_payloads(item):
        for result in payload.get("retrieval_results", []) or []:
            if not isinstance(result, dict):
                continue
            for key in ("source_file", "source_path", "file_name", "path"):
                value = result.get(key)
                if value:
                    files.append(str(value))
            metadata = _as_dict(result.get("metadata"))
            for key in ("source_file", "source_path", "_source"):
                value = metadata.get(key)
                if value:
                    files.append(str(value))
    return files


def _has_suffix(value: str, suffixes: tuple[str, ...]) -> bool:
    return value.lower().strip().endswith(suffixes)


def _render_evidence_matrix_item(
    item: dict[str, Any],
    *,
    root_support_ids: set[str] | None = None,
) -> str:
    raw_data = _as_dict(item.get("raw_data"))
    status = raw_data.get("status") or "unknown"
    summary = (
        str(item.get("fact") or "").strip()
        or str(item.get("summary") or "").strip()
        or str(item.get("uncertainty") or "").strip()
        or "no summary"
    )
    evidence_id = str(item.get("evidence_id") or "unknown")
    citations = _evidence_citation_refs(item)
    rca_role = "root-cause-support" if evidence_id in (root_support_ids or set()) else "context"
    return (
        "- "
        f"id={evidence_id} "
        f"layer={_evidence_layer(item)} "
        f"rca_role={rca_role} "
        f"tool={item.get('source_tool', 'unknown')} "
        f"source={item.get('data_source', 'unknown')} "
        f"type={item.get('evidence_type', 'unknown')} "
        f"stance={item.get('stance', 'neutral')} "
        f"status={status} "
        f"confidence={float(item.get('confidence') or 0.0):.2f} "
        f"citations={_render_inline_list(citations)} "
        f"summary={summary}"
    )


def _evidence_short_ref(item: dict[str, Any]) -> str:
    evidence_id = str(item.get("evidence_id") or "unknown")
    tool = str(item.get("source_tool") or "unknown")
    source = str(item.get("data_source") or "unknown")
    fact = (
        str(item.get("fact") or "").strip()
        or str(item.get("summary") or "").strip()
        or str(item.get("inference") or "").strip()
    )
    return f"{evidence_id} ({tool}/{source}) {fact}".strip()


def _evidence_citation_refs(item: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for payload in _candidate_retrieval_payloads(item):
        for result in payload.get("retrieval_results", []) or []:
            if not isinstance(result, dict):
                continue
            source_file = str(result.get("source_file") or "").strip()
            chunk_id = str(result.get("chunk_id") or "").strip()
            if source_file and chunk_id:
                refs.append(f"{source_file}#{chunk_id}")
            elif source_file:
                refs.append(source_file)
    return _dedupe_inline(refs)[:5]
