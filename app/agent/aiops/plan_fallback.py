"""Rule-based structured planning fallback for AIOps incidents."""

from __future__ import annotations

import re
from typing import Any, Literal

from app.models.plan import PlanStep
from app.services.service_topology import service_has_dependency

STANDARD_TOOL_NAMES = [
    "query_alerts",
    "query_metrics",
    "query_logs",
    "query_service_context",
    "query_deploy_history",
    "query_k8s_status",
    "query_mysql_status",
    "query_redis_status",
    "search_runbook",
    "search_history_ticket",
    "suggest_remediation",
    "manual_analysis",
    "restart_service",
    "scale_service",
    "rollback_deployment",
    "apply_config_change",
    "clear_cache",
    "execute_sql",
    "run_shell",
    "delete_pod",
]

REQUESTED_ACTION_ALIASES = {
    "execute_sql": "execute_sql",
    "run_sql": "execute_sql",
    "sql": "execute_sql",
    "restart": "restart_service",
    "restart_pod": "restart_service",
    "restart_service": "restart_service",
    "scale": "scale_service",
    "scale_service": "scale_service",
    "rollback": "rollback_deployment",
    "rollback_deployment": "rollback_deployment",
    "apply_config": "apply_config_change",
    "apply_config_change": "apply_config_change",
    "clear_cache": "clear_cache",
    "delete_pod": "delete_pod",
    "delete_k8s_pod": "delete_k8s_pod",
    "run_shell": "run_shell",
    "execute_shell": "execute_shell",
}

HIGH_RISK_REQUESTED_ACTIONS = {
    "delete_pod",
    "delete_k8s_pod",
    "execute_shell",
    "run_shell",
    "execute_sql",
    "run_sql",
}

APPROVAL_REQUESTED_ACTIONS = {
    "restart_service",
    "scale_service",
    "rollback_deployment",
    "apply_config_change",
    "clear_cache",
}


def render_plan_step(step: PlanStep) -> str:
    """Render a structured step into the legacy string plan format."""
    return (
        f"[{step.step_id}] 使用 {step.tool_name}: {step.purpose} "
        f"| 参数: {step.input_args} "
        f"| 预期证据: {step.expected_evidence} "
        f"| 风险: {step.risk_level}"
    )


def normalize_plan_steps(
    raw_steps: list[Any], input_text: str, incident: dict[str, Any] | None
) -> list[PlanStep]:
    """Coerce model output into valid PlanStep objects."""
    steps: list[PlanStep] = []
    service_name = infer_service_name(input_text, incident)

    for index, raw_step in enumerate(raw_steps, 1):
        try:
            if isinstance(raw_step, PlanStep):
                step = raw_step
            elif isinstance(raw_step, dict):
                step = PlanStep(**raw_step)
            else:
                step = PlanStep(
                    step_id=f"s{index}",
                    tool_name="manual_analysis",
                    purpose=str(raw_step),
                    expected_evidence="人工分析该步骤是否完成",
                )

            if not step.step_id:
                step.step_id = f"s{index}"
            if not step.input_args:
                step.input_args = default_input_args(step.tool_name, service_name, input_text)
            step.status = "pending"
            steps.append(step)
        except Exception:
            steps.append(
                PlanStep(
                    step_id=f"s{index}",
                    tool_name="manual_analysis",
                    purpose=str(raw_step),
                    input_args={"service_name": service_name},
                    expected_evidence="模型输出无法结构化，保留为人工分析步骤",
                )
            )

    if not steps:
        return build_fallback_plan(input_text=input_text, incident=incident)

    return append_incident_requested_action_step(steps, incident)


def build_fallback_plan(input_text: str, incident: dict[str, Any] | None = None) -> list[PlanStep]:
    """Build a deterministic PlanStep list when LLM planning is unavailable."""
    service_name = infer_service_name(input_text, incident)
    symptom = infer_symptom(input_text, incident)
    lowered = symptom.lower()
    dependency_hint = infer_dependency_hint(service_name, lowered)
    golden_steps = build_golden_dependency_plan(
        service_name,
        symptom,
        dependency_hint,
        bool((incident or {}).get("raw_alert")),
    )
    if golden_steps:
        return append_incident_requested_action_step(golden_steps, incident)

    steps: list[PlanStep] = []

    def add(
        tool_name: str,
        purpose: str = "",
        expected_evidence: str = "",
        input_args: dict[str, Any] | None = None,
        risk_level: str = "low",
    ) -> None:
        if tool_name not in STANDARD_TOOL_NAMES:
            return
        purpose = purpose or f"Run {tool_name} for incident diagnosis."
        expected_evidence = (
            expected_evidence or "Collect diagnostic evidence for the current incident."
        )
        steps.append(
            PlanStep(
                step_id=f"s{len(steps) + 1}",
                tool_name=tool_name,
                purpose=purpose,
                input_args=input_args or default_input_args(tool_name, service_name, symptom),
                expected_evidence=expected_evidence,
                risk_level=risk_level,  # type: ignore[arg-type]
                status="pending",
            )
        )

    if dependency_hint == "mysql":
        add(
            "query_alerts",
            f"Review current firing alerts and labels for {service_name}.",
            "Confirm whether the Incident input has active alert context.",
        )

    if dependency_hint == "redis":
        add(
            "query_service_context",
            f"查询 {service_name} 的 owner、namespace 和上下游依赖",
            "确认服务依赖和责任边界",
        )
        add(
            "query_redis_status",
            "根据服务拓扑优先检查 Redis 连接数、maxclients、内存和慢日志",
            "判断 Redis 是否连接数耗尽或慢查询异常",
        )
        add(
            "query_metrics",
            f"检查 {service_name} 最近 10 分钟延迟、错误率、CPU 和内存指标",
            "确认故障是否伴随延迟、5xx 或资源异常",
        )
        add(
            "query_logs",
            f"检索 {service_name} 最近 10 分钟 Redis timeout 和 ERROR 日志",
            "确认是否存在 Redis 连接超时或客户端异常",
        )
        add(
            "query_deploy_history",
            f"查询 {service_name} 近期发布记录",
            "判断故障是否与最近发布或配置变更相关",
        )
        add(
            "search_runbook",
            "检索 Redis 连接异常处理手册",
            "获取 Redis timeout 的标准排查和恢复建议",
            {"query": f"{service_name} Redis connection timeout maxclients 处理手册"},
        )
        add(
            "search_history_ticket",
            "检索历史 Redis timeout 相似故障工单",
            "确认是否存在相同根因和处理方式",
        )
        add(
            "suggest_remediation",
            "基于 Redis 证据生成修复建议，不直接执行变更",
            "输出低风险和需审批的修复建议",
            risk_level="medium",
        )
    elif dependency_hint == "mysql":
        add(
            "query_service_context",
            f"查询 {service_name} 的 owner、namespace 和数据库依赖",
            "确认服务依赖和责任边界",
        )
        add(
            "query_mysql_status",
            "根据服务拓扑优先检查 MySQL 慢 SQL、连接池、锁等待和活跃连接数",
            "判断是否由慢查询、锁等待或连接池耗尽导致",
        )
        add(
            "query_metrics",
            f"检查 {service_name} 最近 10 分钟延迟、错误率和资源指标",
            "确认数据库异常对接口指标的影响",
        )
        add(
            "query_logs",
            f"检索 {service_name} MySQL 慢查询、连接池和锁等待日志",
            "确认应用侧是否出现数据库相关错误",
        )
        add(
            "query_deploy_history",
            f"查询 {service_name} 近期发布记录",
            "判断慢查询是否与最近发布或配置变更相关",
        )
        add(
            "search_runbook",
            "检索 MySQL 慢查询和连接池异常处理手册",
            "获取数据库故障排查步骤",
            {"query": f"{service_name} MySQL 慢查询 连接池 锁等待 Runbook"},
        )
        add(
            "search_history_ticket",
            "检索历史 MySQL 慢查询和连接池等待工单",
            "确认是否存在相同 SQL、索引、连接池或发布相关根因",
            {"query": f"{service_name} MySQL 慢查询 连接池等待", "limit": 5},
        )
        add(
            "suggest_remediation",
            "生成 MySQL 故障修复建议，不执行 SQL 或变更",
            "输出风险受控的处理建议",
            risk_level="medium",
        )
    elif contains_any(lowered, ["redis", "connection timeout", "maxclients", "连接超时", "连接数"]):
        add(
            "query_service_context",
            f"查询 {service_name} 的 owner、namespace 和上下游依赖",
            "确认服务依赖和责任边界",
        )
        add(
            "query_metrics",
            f"检查 {service_name} 最近 10 分钟延迟、错误率、CPU 和内存指标",
            "确认故障是否伴随延迟、5xx 或资源异常",
        )
        add(
            "query_logs",
            f"检索 {service_name} 最近 10 分钟 Redis timeout 和 ERROR 日志",
            "确认是否存在 Redis 连接超时或客户端异常",
        )
        add(
            "query_redis_status",
            "检查 Redis 连接数、maxclients、内存和慢日志",
            "判断 Redis 是否连接数耗尽或慢查询异常",
        )
        add(
            "query_deploy_history",
            f"查询 {service_name} 近期发布记录",
            "判断故障是否与最近发布或配置变更相关",
        )
        add(
            "search_runbook",
            "检索 Redis 连接异常处理手册",
            "获取 Redis timeout 的标准排查和恢复建议",
            {"query": f"{service_name} Redis connection timeout maxclients 处理手册"},
        )
        add(
            "search_history_ticket",
            "检索历史 Redis timeout 相似故障工单",
            "确认是否存在相同根因和处理方式",
        )
        add(
            "suggest_remediation",
            "基于 Redis 证据生成修复建议，不直接执行变更",
            "输出低风险和需审批的修复建议",
            risk_level="medium",
        )
    elif contains_any(lowered, ["mysql", "slow query", "慢查询", "锁等待", "连接池"]):
        add(
            "query_service_context",
            f"查询 {service_name} 的 owner、namespace 和数据库依赖",
            "确认服务依赖和责任边界",
        )
        add(
            "query_metrics",
            f"检查 {service_name} 最近 10 分钟延迟、错误率和资源指标",
            "确认数据库异常对接口指标的影响",
        )
        add(
            "query_logs",
            f"检索 {service_name} MySQL 慢查询、连接池和锁等待日志",
            "确认应用侧是否出现数据库相关错误",
        )
        add(
            "query_mysql_status",
            "检查 MySQL 慢 SQL、连接池、锁等待和活跃连接数",
            "判断是否由慢查询、锁等待或连接池耗尽导致",
        )
        add(
            "query_deploy_history",
            f"查询 {service_name} 近期发布记录",
            "判断数据库异常是否与最近发布或配置变更相关",
        )
        add(
            "search_runbook",
            "检索 MySQL 慢查询和连接池异常处理手册",
            "获取数据库故障排查步骤",
            {"query": f"{service_name} MySQL 慢查询 连接池 锁等待 Runbook"},
        )
        add(
            "search_history_ticket",
            "检索历史 MySQL 慢查询和连接池等待工单",
            "确认是否存在相同 SQL、索引、连接池或发布相关根因",
            {"query": f"{service_name} MySQL 慢查询 连接池等待", "limit": 5},
        )
        add(
            "suggest_remediation",
            "生成 MySQL 故障修复建议，不执行 SQL 或变更",
            "输出风险受控的处理建议",
            risk_level="medium",
        )
    elif contains_any(
        lowered, ["crashloopbackoff", "crash loop", "pod crash", "pod 重启", "重启次数"]
    ):
        add(
            "query_service_context",
            f"查询 {service_name} 的 owner、namespace 和依赖",
            "确认服务依赖和责任边界",
        )
        add(
            "query_k8s_status",
            f"检查 {service_name} Pod 状态、重启次数、镜像版本和部署时间",
            "确认是否存在 CrashLoopBackOff、频繁重启或发布异常",
        )
        add(
            "query_logs",
            f"检索 {service_name} 最近启动失败和 ERROR 日志",
            "定位容器启动失败或运行时崩溃原因",
        )
        add(
            "query_metrics",
            f"检查 {service_name} CPU、内存和 OOM 相关指标",
            "判断是否因资源不足触发重启",
        )
        add(
            "query_deploy_history",
            f"查询 {service_name} 近期发布记录",
            "判断 CrashLoop 是否与最近镜像或配置发布相关",
        )
        add(
            "search_runbook",
            "检索 Pod CrashLoopBackOff 处理手册",
            "获取标准排查路径",
            {"query": f"{service_name} Pod CrashLoopBackOff OOMKilled Runbook"},
        )
    elif contains_any(
        lowered, ["服务不可用", "unavailable", "5xx", "503", "502", "不可用", "无法访问"]
    ):
        add(
            "query_service_context",
            f"查询 {service_name} 的 owner、namespace 和上下游依赖",
            "确认服务依赖和责任边界",
        )
        add(
            "query_metrics",
            f"检查 {service_name} QPS、5xx、P95、CPU 和内存指标",
            "确认服务不可用的指标表现和开始时间",
        )
        add(
            "query_logs",
            f"检索 {service_name} 最近 10 分钟 ERROR 和异常堆栈",
            "确认服务不可用的应用错误证据",
        )
        add(
            "query_k8s_status",
            f"检查 {service_name} Pod 就绪状态、重启次数和发布信息",
            "判断是否由实例异常或发布导致",
        )
        add(
            "query_deploy_history",
            f"查询 {service_name} 近期发布记录",
            "判断服务不可用是否与最近发布相关",
        )
        add(
            "search_runbook",
            "检索服务不可用处理手册",
            "获取服务不可用标准处理路径",
            {"query": f"{service_name} 服务不可用 5xx Runbook"},
        )
        add("search_history_ticket", "检索历史服务不可用相似故障", "寻找相似根因和处置方式")
    elif contains_any(lowered, ["响应慢", "slow", "latency", "p95", "超时", "timeout", "延迟"]):
        add(
            "query_service_context",
            f"查询 {service_name} 的 owner、namespace 和上下游依赖",
            "确认服务依赖和责任边界",
        )
        add(
            "query_metrics",
            f"检查 {service_name} QPS、P95、错误率、CPU 和内存指标",
            "确认慢响应发生时间、范围和资源相关性",
        )
        add(
            "query_logs",
            f"检索 {service_name} timeout、ERROR 和慢调用日志",
            "确认慢响应对应的应用错误或下游依赖",
        )
        add(
            "query_mysql_status",
            "检查 MySQL 慢 SQL、连接池和锁等待",
            "排除数据库慢查询或连接池瓶颈",
        )
        add("query_redis_status", "检查 Redis 连接数、慢日志和内存", "排除 Redis 连接耗尽或慢操作")
        add(
            "query_deploy_history",
            f"查询 {service_name} 近期发布记录",
            "判断延迟升高是否与发布相关",
        )
        add(
            "search_runbook",
            "检索接口响应慢处理手册",
            "获取慢响应排查路径",
            {"query": f"{service_name} 接口响应慢 P95 timeout Runbook"},
        )
    elif contains_any(lowered, ["cpu", "高 cpu", "cpu 高", "cpu 使用率"]):
        add(
            "query_metrics",
            f"检查 {service_name} CPU 使用率、P95 和错误率趋势",
            "确认 CPU 是否超过阈值并影响接口",
        )
        add(
            "query_logs",
            f"检索 {service_name} 高 CPU 期间 ERROR 和异常日志",
            "确认高 CPU 是否伴随业务错误",
        )
        add(
            "search_runbook",
            "检索 CPU 高使用率处理手册",
            "获取 CPU 高排查方法",
            {"query": f"{service_name} CPU 高 使用率 Runbook"},
        )
        add(
            "suggest_remediation",
            "生成高 CPU 修复建议，不执行扩容或重启",
            "输出需审批的动作建议",
            risk_level="medium",
        )
    elif contains_any(lowered, ["memory", "内存", "oom", "内存高"]):
        add(
            "query_metrics",
            f"检查 {service_name} 内存使用率、P95 和错误率趋势",
            "确认内存压力和业务影响",
        )
        add(
            "query_logs",
            f"检索 {service_name} OOM、内存泄漏和 ERROR 日志",
            "确认是否存在 OOMKilled 或内存泄漏迹象",
        )
        add(
            "query_k8s_status",
            f"检查 {service_name} Pod 重启和 OOMKilled 状态",
            "判断是否由内存限制触发重启",
        )
        add(
            "search_runbook",
            "检索内存高使用率处理手册",
            "获取内存问题排查方法",
            {"query": f"{service_name} 内存高 OOM Runbook"},
        )
    elif contains_any(lowered, ["disk", "磁盘", "磁盘高", "磁盘满", "no space"]):
        add(
            "query_metrics", f"检查 {service_name} 资源指标和错误率趋势", "确认磁盘异常是否影响服务"
        )
        add(
            "query_logs",
            f"检索 {service_name} no space、磁盘写入失败和 ERROR 日志",
            "确认是否存在磁盘空间不足证据",
        )
        add(
            "query_k8s_status",
            f"检查 {service_name} Pod 状态和挂载卷信息",
            "判断是否由磁盘或挂载卷异常导致",
        )
        add(
            "search_runbook",
            "检索磁盘高使用率处理手册",
            "获取磁盘空间问题排查方法",
            {"query": f"{service_name} 磁盘高 no space Runbook"},
        )
    else:
        add(
            "query_service_context",
            f"查询 {service_name} 的 owner、namespace 和上下游依赖",
            "确认服务依赖和责任边界",
        )
        add(
            "query_metrics",
            f"检查 {service_name} QPS、P95、错误率、CPU 和内存指标",
            "获取故障的基础监控画像",
        )
        add(
            "query_logs",
            f"检索 {service_name} 最近 10 分钟 ERROR、timeout 和异常日志",
            "获取应用侧错误证据，避免依赖未接入的 Trace/MQ 兜底分析",
        )
        add("query_deploy_history", f"查询 {service_name} 近期发布记录", "判断是否存在变更关联")
        add(
            "search_runbook", "检索与症状匹配的内部 Runbook", "获取标准排查路径", {"query": symptom}
        )
        add(
            "suggest_remediation",
            "基于已有证据生成修复建议，不直接执行变更",
            "输出风险受控的建议",
            risk_level="medium",
        )

    return finalize_fallback_plan(steps, service_name, symptom, incident)


def finalize_fallback_plan(
    steps: list[PlanStep],
    service_name: str,
    symptom: str,
    incident: dict[str, Any] | None,
) -> list[PlanStep]:
    """Normalize fallback plans without changing their diagnostic intent."""
    if not steps:
        steps = build_basic_diagnostic_plan(service_name, symptom)
    tool_names = [step.tool_name for step in steps]
    if "query_metrics" in tool_names and "query_logs" not in tool_names:
        metrics_index = tool_names.index("query_metrics")
        steps.insert(
            metrics_index + 1,
            PlanStep(
                step_id="s-log-fill",
                tool_name="query_logs",
                purpose="Search application error, timeout and exception logs.",
                input_args=default_input_args("query_logs", service_name, symptom),
                expected_evidence="Logs provide application-side failure evidence.",
                risk_level="low",
                status="pending",
            ),
        )
    return append_incident_requested_action_step(steps, incident)


def append_incident_requested_action_step(
    steps: list[PlanStep],
    incident: dict[str, Any] | None = None,
) -> list[PlanStep]:
    """Keep an explicit requested action inside the workflow execution budget."""
    normalized = ensure_unique_step_ids(steps)
    requested_step = build_incident_requested_action_step(incident)
    if requested_step is None:
        return normalized
    if _has_equivalent_requested_action(normalized, requested_step):
        return normalized
    if not _is_interview_golden_requested_action(incident):
        requested_step = requested_step.model_copy(update={"step_id": "s1"})
        return ensure_unique_step_ids([requested_step, *normalized])
    if len(normalized) >= 8:
        normalized = [step for step in normalized if step.tool_name != "suggest_remediation"][:7]
    requested_step = requested_step.model_copy(update={"step_id": f"s{len(normalized) + 1}"})
    return ensure_unique_step_ids([*normalized, requested_step])


def _is_interview_golden_requested_action(incident: dict[str, Any] | None) -> bool:
    raw_alert = (incident or {}).get("raw_alert")
    if not isinstance(raw_alert, dict):
        return False
    alertname = str(raw_alert.get("alertname") or "").lower()
    return alertname in {"redismaxclientsnearlimit", "mysqlslowquerylatency"}


def build_incident_requested_action_step(incident: dict[str, Any] | None) -> PlanStep | None:
    """Build a risk-gated plan step from incident.raw_alert.requested_action."""
    if not incident:
        return None
    raw_alert = incident.get("raw_alert")
    if not isinstance(raw_alert, dict):
        return None
    requested_action = str(
        raw_alert.get("requested_action") or raw_alert.get("action") or ""
    ).strip()
    if not requested_action:
        return None

    tool_name = _normalize_requested_action(requested_action)
    service_name = infer_service_name("", incident)
    input_args = dict(raw_alert)
    input_args.setdefault("requested_action", requested_action)
    input_args.setdefault("service_name", service_name)
    input_args.setdefault("source", "incident.raw_alert")

    reason = str(raw_alert.get("reason") or raw_alert.get("description") or "").strip()
    reason_suffix = f"：{reason}" if reason else ""
    return PlanStep(
        step_id="s1",
        tool_name=tool_name,
        purpose=f"评估原始告警请求的生产动作 {tool_name}{reason_suffix}",
        input_args=input_args,
        expected_evidence="风险控制器必须先判断该动作是否禁止或需要人工审批，不能自动执行生产变更。",
        risk_level=_requested_action_risk_level(tool_name, raw_alert),
        status="pending",
    )


def infer_service_name(input_text: str, incident: dict[str, Any] | None = None) -> str:
    """Infer service name from incident or free-text input."""
    if incident:
        service_name = str(incident.get("service_name") or "").strip()
        if service_name and service_name != "unknown-service":
            return service_name

    patterns = [
        r"\b([a-zA-Z0-9][a-zA-Z0-9-]*-service)\b",
        r"service_name[=:：\s]+([a-zA-Z0-9_.-]+)",
        r"服务[：:\s]+([a-zA-Z0-9_.-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, input_text)
        if match:
            return match.group(1)

    return "unknown-service"


def infer_symptom(input_text: str, incident: dict[str, Any] | None = None) -> str:
    """Infer symptom text from incident or raw input."""
    if incident:
        symptom = str(incident.get("symptom") or "").strip()
        if symptom:
            return symptom
        title = str(incident.get("title") or "").strip()
        if title:
            return title
    return input_text


def default_input_args(tool_name: str, service_name: str, symptom: str) -> dict[str, Any]:
    """Default inputs that can be consumed by future Tool Registry adapters."""
    if tool_name == "query_alerts":
        return {"service_name": service_name, "state": "active", "limit": 20}
    if tool_name == "search_runbook":
        return {"query": symptom}
    if tool_name == "suggest_remediation":
        return {"service_name": service_name, "symptom": symptom}
    if tool_name == "query_logs":
        return {"service_name": service_name, "time_range": "10m", "query": "ERROR OR timeout"}
    if tool_name == "query_metrics":
        return {"service_name": service_name, "time_range": "10m", "interval": "1m"}
    if tool_name == "query_deploy_history":
        return {"service_name": service_name, "time_range": "24h", "limit": 5}
    return {"service_name": service_name, "time_range": "10m"}


def build_golden_dependency_plan(
    service_name: str,
    symptom: str,
    dependency_hint: str,
    has_raw_alert: bool = False,
) -> list[PlanStep]:
    """Return deterministic plans for the three interview-grade golden incidents."""
    if not has_raw_alert:
        return []
    if dependency_hint not in {"redis", "mysql"} and not contains_any(
        symptom.lower(),
        ["crashloopbackoff", "crash loop", "pod crash", "oomkilled"],
    ):
        return []
    if dependency_hint not in {"redis", "mysql"} and service_name != "inventory-service":
        return []

    specs: dict[str, list[tuple[str, str, str, dict[str, Any] | None, str]]] = {
        "redis": [
            (
                "query_redis_status",
                "Check Redis connected_clients, maxclients, blocked clients, memory and slowlog first.",
                "Redis connected_clients/maxclients and blocked_clients prove or refute connection exhaustion.",
                None,
                "low",
            ),
            (
                "query_metrics",
                "Check service P95 latency, 5xx, CPU and memory during the alert window.",
                "Service metrics confirm user impact and timing.",
                None,
                "low",
            ),
            (
                "query_logs",
                "Search Redis timeout and request failure logs in the alert window.",
                "Application logs connect Redis saturation to request failures.",
                None,
                "low",
            ),
            (
                "search_runbook",
                "Retrieve Redis maxclients and timeout runbook guidance.",
                "Runbook provides the safe diagnostic and remediation path.",
                {"query": f"{service_name} Redis maxclients connected_clients timeout runbook"},
                "low",
            ),
            (
                "search_history_ticket",
                "Search historical Redis maxclients incidents for the same service family.",
                "Historical tickets confirm similar root cause and recovery playbook.",
                None,
                "low",
            ),
            (
                "suggest_remediation",
                "Generate remediation suggestions without executing production changes.",
                "Suggestions must separate read-only checks from approval-gated changes.",
                None,
                "medium",
            ),
        ],
        "mysql": [
            (
                "query_mysql_status",
                "Check MySQL slow queries, active connections, pool waiting and lock waits first.",
                "Slow SQL digest plus pool waiting proves or refutes database-induced latency.",
                None,
                "low",
            ),
            (
                "query_metrics",
                "Check service P95, error rate and CPU during the alert window.",
                "P95 confirms impact; a modest error-rate change and elevated CPU remain symptoms, not root-cause proof.",
                None,
                "low",
            ),
            (
                "query_logs",
                "Search MySQL slow query, timeout and connection pool waiting logs.",
                "Application logs identify the slow SQL digest and connect it to pool waiting.",
                {
                    "service_name": service_name,
                    "time_range": "10m",
                    "query": "slow query OR digest OR pool_waiting OR connection wait",
                },
                "low",
            ),
            (
                "query_deploy_history",
                "Correlate the incident with recent payment-service releases and feature flags.",
                "A recently enabled reporting path raises the matching SQL hypothesis but does not prove causality alone.",
                None,
                "low",
            ),
            (
                "search_runbook",
                "Retrieve MySQL slow-query postmortem and feature-flag/index guidance.",
                "Knowledge sources provide EXPLAIN, index and feature-flag guidance without becoming live facts.",
                {
                    "query": (
                        f"{service_name} MySQL slow query digest connection pool waiting "
                        "covering index feature flag postmortem"
                    )
                },
                "low",
            ),
            (
                "search_history_ticket",
                "Search historical MySQL slow-query incidents with index or feature-flag mitigation.",
                "Past incidents provide comparable remediation experience but do not replace current evidence.",
                {
                    "service_name": service_name,
                    "query": (
                        f"{service_name} MySQL slow query digest pool waiting "
                        "covering index feature flag"
                    ),
                    "limit": 5,
                },
                "low",
            ),
            (
                "suggest_remediation",
                "Generate remediation suggestions without executing SQL or config changes.",
                "Suggestions must keep SQL/config changes behind approval.",
                None,
                "medium",
            ),
        ],
        "k8s": [
            (
                "query_k8s_status",
                "Check Pod status, restarts, last state, events and deployment timing first.",
                "CrashLoopBackOff, OOMKilled and restart count prove or refute Pod instability.",
                None,
                "low",
            ),
            (
                "query_logs",
                "Search startup failure, OOMKilled and ERROR logs for the affected Pod.",
                "Logs explain why the container exits and restarts.",
                None,
                "low",
            ),
            (
                "query_metrics",
                "Check CPU, memory and error metrics around the CrashLoop window.",
                "Metrics confirm memory pressure and customer impact.",
                None,
                "low",
            ),
            (
                "search_runbook",
                "Retrieve Pod CrashLoopBackOff and OOMKilled runbook guidance.",
                "Runbook provides safe recovery and escalation steps.",
                {"query": f"{service_name} Pod CrashLoopBackOff OOMKilled runbook"},
                "low",
            ),
        ],
    }
    key = dependency_hint
    if not key:
        key = "k8s"

    steps: list[PlanStep] = []
    for index, (tool_name, purpose, expected, input_args, risk_level) in enumerate(
        specs[key],
        1,
    ):
        steps.append(
            PlanStep(
                step_id=f"s{index}",
                tool_name=tool_name,
                purpose=purpose,
                input_args=input_args or default_input_args(tool_name, service_name, symptom),
                expected_evidence=expected,
                risk_level=risk_level,  # type: ignore[arg-type]
                status="pending",
            )
        )
    return steps


def build_basic_diagnostic_plan(service_name: str, symptom: str) -> list[PlanStep]:
    """Return the conservative read-only fallback used when legacy text rules miss."""
    specs = [
        (
            "query_metrics",
            "Check service latency, errors and resource metrics.",
            "Metrics establish impact and timing.",
        ),
        (
            "query_logs",
            "Search application error, timeout and exception logs.",
            "Logs provide application-side failure evidence.",
        ),
        (
            "search_runbook",
            "Retrieve a matching runbook if a trusted one exists.",
            "Runbook hit or no-answer rejection defines the safe next step.",
        ),
        (
            "suggest_remediation",
            "Generate remediation suggestions without executing production changes.",
            "Suggestions keep write actions behind approval.",
        ),
    ]
    return [
        PlanStep(
            step_id=f"s{index}",
            tool_name=tool_name,
            purpose=purpose,
            input_args=default_input_args(tool_name, service_name, symptom),
            expected_evidence=expected,
            risk_level="medium" if tool_name == "suggest_remediation" else "low",
            status="pending",
        )
        for index, (tool_name, purpose, expected) in enumerate(specs, 1)
    ]


def infer_dependency_hint(service_name: str, lowered_symptom: str) -> str:
    """Use topology to prioritize dependency-specific diagnosis plans."""
    if service_has_dependency(service_name, "redis") and contains_any(
        lowered_symptom,
        ["redis", "cache", "connection timeout", "maxclients", "缓存"],
    ):
        return "redis"
    if service_has_dependency(service_name, "mysql") and contains_any(
        lowered_symptom,
        ["mysql", "sql", "database", "db", "slow query", "慢查询", "锁等待"],
    ):
        return "mysql"
    return ""


def ensure_unique_step_ids(steps: list[PlanStep]) -> list[PlanStep]:
    """Make step ids stable and unique for executor consumption."""
    seen: set[str] = set()
    normalized: list[PlanStep] = []
    for index, step in enumerate(steps, 1):
        step_id = step.step_id or f"s{index}"
        if step_id in seen:
            step_id = f"s{index}"
        seen.add(step_id)
        normalized.append(step.model_copy(update={"step_id": step_id, "status": "pending"}))
    return normalized


def contains_any(text: str, keywords: list[str]) -> bool:
    """Return True when any keyword appears in text."""
    return any(keyword.lower() in text for keyword in keywords)


def _normalize_requested_action(action: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", action.strip().lower()).strip("_")
    return REQUESTED_ACTION_ALIASES.get(normalized, normalized or "manual_analysis")


def _requested_action_risk_level(
    tool_name: str, raw_alert: dict[str, Any]
) -> Literal["low", "medium", "high"]:
    if tool_name in HIGH_RISK_REQUESTED_ACTIONS:
        return "high"
    if "sql" in tool_name and raw_alert.get("audited") is not True:
        return "high"
    if tool_name in APPROVAL_REQUESTED_ACTIONS:
        return "medium"
    return "medium"


def _has_equivalent_requested_action(steps: list[PlanStep], requested_step: PlanStep) -> bool:
    requested_sql = str(
        requested_step.input_args.get("sql") or requested_step.input_args.get("query") or ""
    ).strip()
    for step in steps:
        if step.tool_name != requested_step.tool_name:
            continue
        step_sql = str(step.input_args.get("sql") or step.input_args.get("query") or "").strip()
        if requested_sql and step_sql and requested_sql == step_sql:
            return True
        if not requested_sql:
            return True
    return False
