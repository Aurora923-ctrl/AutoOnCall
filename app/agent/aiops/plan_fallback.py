"""Rule-based structured planning fallback for AIOps incidents."""

from __future__ import annotations

import re
from typing import Any, Literal

from app.models.plan import PlanStep
from app.services.service_topology import get_primary_dependency_instance, service_has_dependency

STANDARD_TOOL_NAMES = [
    "query_alerts",
    "query_metrics",
    "query_logs",
    "query_traces",
    "query_service_context",
    "query_deploy_history",
    "query_message_queue_status",
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

    steps: list[PlanStep] = []

    def add(
        tool_name: str,
        purpose: str,
        expected_evidence: str,
        input_args: dict[str, Any] | None = None,
        risk_level: str = "low",
    ) -> None:
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

    add(
        "query_alerts",
        f"回查 {service_name} 当前 firing 告警和告警标签",
        "确认 Incident 输入是否有实时告警支撑",
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
            "query_traces",
            f"查询 {service_name} 最近调用链中的错误 span 和慢 span",
            "判断 Redis 超时是否沿调用链传播",
        )
        add(
            "query_message_queue_status",
            f"检查 {service_name} 关联 Kafka/Redpanda topic 和 partition 状态",
            "排除消息积压或分区异常导致的下游超时",
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
            "query_traces",
            f"查询 {service_name} 数据库调用链和慢 span",
            "判断慢查询是否出现在关键调用路径",
        )
        add(
            "query_message_queue_status",
            f"检查 {service_name} 关联 Kafka/Redpanda topic 和 partition 状态",
            "排除消息消费积压放大数据库延迟",
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
        add("query_traces", f"查询 {service_name} 调用链中 Redis 相关慢 span", "判断超时传播路径")
        add(
            "query_message_queue_status",
            f"检查 {service_name} 关联 Kafka/Redpanda topic 和 partition 状态",
            "排除消息积压或分区异常导致的下游超时",
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
            "query_traces", f"查询 {service_name} 数据库调用链和慢 span", "判断数据库慢调用影响范围"
        )
        add(
            "query_message_queue_status",
            f"检查 {service_name} 关联 Kafka/Redpanda topic 和 partition 状态",
            "排除消息消费积压放大数据库延迟",
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
            "query_traces",
            f"查询 {service_name} 服务不可用期间调用链错误传播",
            "判断是否由下游依赖或自身错误导致",
        )
        add(
            "query_message_queue_status",
            f"检查 {service_name} 关联 Kafka/Redpanda topic 和 partition 状态",
            "判断服务不可用是否伴随消息积压或分区异常",
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
        add("query_traces", f"查询 {service_name} 慢 span 和下游调用耗时", "定位慢响应传播路径")
        add(
            "query_message_queue_status",
            f"检查 {service_name} 关联 Kafka/Redpanda topic 和 partition 状态",
            "判断慢响应是否由消息积压或消费延迟放大",
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
        add("query_logs", f"检索 {service_name} 最近 10 分钟 ERROR 日志", "获取应用错误证据")
        add("query_traces", f"查询 {service_name} 最近调用链摘要", "补充调用链视角的错误和耗时证据")
        add(
            "query_message_queue_status",
            f"检查 {service_name} 关联 Kafka/Redpanda topic 和 partition 状态",
            "补充消息队列依赖健康证据",
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

    return append_incident_requested_action_step(steps, incident)


def append_incident_requested_action_step(
    steps: list[PlanStep],
    incident: dict[str, Any] | None = None,
) -> list[PlanStep]:
    """Append an explicit raw_alert requested action so risk control cannot miss it."""
    normalized = ensure_unique_step_ids(steps)
    requested_step = build_incident_requested_action_step(incident)
    if requested_step is None:
        return normalized
    if _has_equivalent_requested_action(normalized, requested_step):
        return normalized
    requested_step = requested_step.model_copy(update={"step_id": f"s{len(normalized) + 1}"})
    return ensure_unique_step_ids([*normalized, requested_step])


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
    if tool_name == "query_traces":
        return {"service_name": service_name, "lookback": "1h", "limit": 20}
    if tool_name == "query_deploy_history":
        return {"service_name": service_name, "time_range": "24h", "limit": 5}
    if tool_name == "query_message_queue_status":
        return {"service_name": service_name, "topic": infer_message_queue_topic(service_name)}
    return {"service_name": service_name, "time_range": "10m"}


def infer_message_queue_topic(service_name: str) -> str:
    """Infer a stable demo topic name from topology or service name."""
    configured = get_primary_dependency_instance(
        service_name, "kafka"
    ) or get_primary_dependency_instance(
        service_name,
        "redpanda",
    )
    if configured:
        return configured
    normalized = service_name.removesuffix("-service").replace("_", "-").strip("-")
    if not normalized or normalized == "unknown":
        return ""
    return f"redpanda-{normalized}"


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
