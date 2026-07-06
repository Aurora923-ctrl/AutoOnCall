"""Shared diagnostic signal rules for evidence and root-cause analysis."""

from __future__ import annotations

from typing import Any

from app.services.diagnostic_signal_catalog import (
    DATA_SOURCE_ALIASES,
    DIAGNOSTIC_SIGNAL_CANDIDATES,
    FALLBACK_EXECUTION_PATHS,
    KNOWN_EVIDENCE_SOURCES,
)


def tool_payload_output(raw_data: dict[str, Any]) -> Any:
    """Return tool output from a raw ToolExecutionResult payload."""
    output = raw_data.get("output")
    return output if output is not None else raw_data


def evidence_output(evidence: Any) -> Any:
    """Return the nested output stored in one Evidence dict."""
    if not isinstance(evidence, dict):
        return None
    raw_data = evidence.get("raw_data") or {}
    if not isinstance(raw_data, dict):
        return None
    return raw_data.get("output")


def normalize_data_source(source_tool: str, raw_data: dict[str, Any] | None = None) -> str:
    """Return a stable provenance label for reports, traces, and UI badges."""
    payload = raw_data or {}
    output = tool_payload_output(payload)
    source = ""
    if isinstance(output, dict):
        source = str(output.get("source") or "")
        synthetic_fields = output.get("synthetic_fields")
        if source.strip().lower() == "mcp_monitor" and synthetic_fields:
            return "mcp_monitor_mixed"
    if not source:
        source = str(payload.get("source") or "")

    error_type = ""
    if isinstance(output, dict):
        error_type = str(output.get("error_type") or "")
    error_type = error_type or str(payload.get("error_type") or "")
    if error_type == "not_configured":
        return "not_configured"

    output_status = str(output.get("status") or "") if isinstance(output, dict) else ""
    payload_status = str(payload.get("status") or "")
    if output_status.lower() == "failed" or payload_status.lower() == "failed":
        return "failed"

    normalized = source.strip().lower()
    if normalized in DATA_SOURCE_ALIASES:
        return DATA_SOURCE_ALIASES[normalized]
    if normalized in KNOWN_EVIDENCE_SOURCES:
        return normalized

    metadata = payload.get("metadata")
    execution_path = metadata.get("execution_path") if isinstance(metadata, dict) else ""
    if execution_path in FALLBACK_EXECUTION_PATHS:
        return str(execution_path)

    tool_name = source_tool.lower()
    if "knowledge" in tool_name or "runbook" in tool_name:
        return "rag"
    return "unknown"


def infer_evidence_type(source_tool: str) -> str:
    """Infer a stable evidence domain from the producing tool name."""
    tool_name = source_tool.lower()
    if "metric" in tool_name:
        return "metric"
    if "log" in tool_name:
        return "log"
    if "runbook" in tool_name or "knowledge" in tool_name:
        return "runbook"
    if "k8s" in tool_name or "kubernetes" in tool_name or "pod" in tool_name:
        return "k8s"
    if "redis" in tool_name:
        return "redis"
    if "mysql" in tool_name or "sql" in tool_name:
        return "mysql"
    if "ticket" in tool_name or "history" in tool_name:
        if "deploy" in tool_name:
            return "change"
        return "ticket"
    if "alert" in tool_name:
        return "alert"
    if "trace" in tool_name or "span" in tool_name:
        return "trace"
    if "queue" in tool_name or "kafka" in tool_name or "redpanda" in tool_name:
        return "message_queue"
    if "context" in tool_name or "cmdb" in tool_name:
        return "service_context"
    if "risk" in tool_name or "approval" in tool_name:
        return "risk"
    return "unknown"


def infer_evidence_stance(
    *,
    source_tool: str,
    raw_data: dict[str, Any],
    summary: str = "",
) -> str:
    """Infer whether evidence supports, refutes, neutrally describes, or cannot judge."""
    if not isinstance(raw_data, dict):
        return "unknown"
    if raw_data.get("status") == "failed":
        return "unknown"

    evidence_type = infer_evidence_type(source_tool)
    output = tool_payload_output(raw_data)
    if isinstance(output, dict) and output.get("status") == "failed":
        return "unknown"
    text = f"{summary} {output}".lower()

    if evidence_type == "metric":
        if is_metric_abnormal(output) or mentions_any(text, ["p95", "5xx", "high", "超过阈值"]):
            return "supporting"
        return "refuting"
    if evidence_type == "log":
        if mentions_any(text, ["error", "timeout", "5xx", "exception", "oom", "异常", "失败"]):
            return "supporting"
        if mentions_any(text, ["0 条", "no error", "empty", "未发现", "正常"]):
            return "refuting"
        return "neutral"
    if evidence_type == "redis":
        if is_redis_abnormal(output) or mentions_any(
            text,
            ["连接数耗尽", "接近上限", "blocked_clients", "慢日志异常", "exhausted"],
        ):
            return "supporting"
        return "refuting"
    if evidence_type == "k8s":
        if mentions_any(text, ["crashloop", "oomkilled", "notready", "restart", "重启", "异常"]):
            return "supporting"
        if mentions_any(text, ["running", "未发现", "正常"]):
            return "refuting"
        return "neutral"
    if evidence_type == "mysql":
        if isinstance(output, dict) and output.get("slow_queries"):
            return "supporting"
        if mentions_any(text, ["slow query", "慢查询", "lock", "锁等待", "pool_waiting"]):
            return "supporting"
        return "neutral"
    if evidence_type == "service_context":
        return (
            "supporting"
            if mentions_any(text, ["dependencies", "依赖", "owner", "namespace"])
            else "neutral"
        )
    if evidence_type == "change":
        return (
            "supporting"
            if mentions_any(text, ["deploy", "发布", "change", "变更", "rollback"])
            else "neutral"
        )
    if evidence_type == "alert":
        return (
            "supporting"
            if mentions_any(text, ["alert", "告警", "firing", "critical"])
            else "neutral"
        )
    if evidence_type == "trace":
        return (
            "supporting"
            if mentions_any(text, ["error_span", "slow", "慢", "错误", "timeout"])
            else "neutral"
        )
    if evidence_type == "message_queue":
        if is_message_queue_healthy(output) or mentions_any(
            text,
            ["无 consumer lag", "topic 正常", "正常"],
        ):
            return "refuting"
        if is_message_queue_abnormal(output) or mentions_any(
            text,
            [
                "consumer lag 高",
                "max_partition_lag",
                "lagging",
                "积压",
                "rebalance",
                "under_replicated",
            ],
        ):
            return "supporting"
        return "neutral"
    if evidence_type in {"runbook", "ticket", "risk"}:
        return "supporting" if text.strip() else "neutral"
    return "unknown"


def build_confidence_reason(
    *,
    source_tool: str,
    raw_data: dict[str, Any],
    stance: str,
) -> str:
    """Build a short explanation for an evidence confidence score."""
    if raw_data.get("status") == "failed":
        return f"工具失败: {raw_data.get('error_message') or '未返回可用数据'}"

    evidence_type = infer_evidence_type(source_tool)
    output = tool_payload_output(raw_data)
    if evidence_type == "metric":
        return "指标阈值命中" if stance == "supporting" else "指标未命中异常阈值"
    if evidence_type == "log":
        return "日志关键词命中" if stance == "supporting" else "日志未发现关键异常"
    if evidence_type == "redis":
        return (
            "Redis 连接数或慢日志阈值命中"
            if stance == "supporting"
            else "Redis 状态与异常假设不一致"
        )
    if evidence_type == "k8s":
        return "K8s 状态异常关键词命中" if stance == "supporting" else "K8s 状态未支持该假设"
    if evidence_type == "mysql":
        return (
            "MySQL 慢查询或连接池信号命中" if stance == "supporting" else "MySQL 未形成明确异常信号"
        )
    if evidence_type == "runbook":
        if isinstance(output, dict) and output.get("no_answer_rejected"):
            return "Runbook 无可信命中，触发拒答"
        return "Runbook 检索命中"
    if evidence_type == "ticket":
        return "历史工单相似根因命中"
    if evidence_type == "alert":
        return "告警平台返回当前 Incident 上下文"
    if evidence_type == "trace":
        return "Tracing 后端返回调用链耗时和错误 span 信号"
    if evidence_type == "message_queue":
        return "消息队列后端返回 topic/partition 状态"
    if evidence_type == "service_context":
        return "服务依赖和责任人上下文已确认"
    if evidence_type == "change":
        return "发布/变更记录用于时间线关联"
    if evidence_type == "risk":
        return "风险策略规则命中"
    if stance == "unknown":
        return "未归类或不可用证据，无法判断证据立场"
    return "未归类证据，按中性处理"


def signal_context(evidence_items: list[Any], input_text: str, incident: Any) -> str:
    """Build shared context text for diagnostic signal matching."""
    incident_text = ""
    if isinstance(incident, dict):
        incident_text = " ".join(
            str(incident.get(key, ""))
            for key in ["title", "service_name", "severity", "symptom", "environment"]
        )
    return " ".join(
        [
            input_text,
            incident_text,
            " ".join(
                str(item.get("summary", "")) for item in evidence_items if isinstance(item, dict)
            ),
            " ".join(
                str(evidence_output(item)) for item in evidence_items if isinstance(item, dict)
            ),
        ]
    ).lower()


def build_signal_hypotheses(evidence_items: list[Any], input_text: str, incident: Any) -> list[str]:
    """Build coarse root-cause hypotheses from shared diagnostic signal rules."""
    joined_text = " ".join(
        [
            input_text,
            str(incident.get("symptom", "")) if isinstance(incident, dict) else "",
            " ".join(
                str(item.get("summary", "")) for item in evidence_items if isinstance(item, dict)
            ),
        ]
    ).lower()

    hypotheses: list[str] = []
    if has_redis_exhaustion_signal(evidence_items, joined_text):
        hypotheses.append(
            "Redis 连接数接近或达到 maxclients，导致应用侧 Redis connection timeout，并放大接口延迟和 5xx。"
        )
    if has_metrics_degradation_signal(evidence_items, joined_text):
        hypotheses.append("服务 P95 延迟或 5xx 错误率异常升高，用户请求链路已受到影响。")
    if has_log_timeout_signal(evidence_items, joined_text):
        hypotheses.append("错误日志出现 timeout 或下游缓存异常，可作为故障症状和调用链证据。")
    if has_mysql_signal(evidence_items, joined_text):
        hypotheses.append("MySQL 慢查询或连接池等待可能参与放大请求延迟。")
    if has_k8s_signal(evidence_items, joined_text):
        hypotheses.append("Kubernetes Pod 状态异常可能导致服务实例容量或稳定性下降。")
    if has_message_queue_lag_signal(evidence_items, joined_text):
        hypotheses.append("Redpanda/Kafka 消费积压或分区异常可能放大请求延迟和下游处理延迟。")
    return dedupe_strings(hypotheses)


def evidence_matches_category(
    category: str,
    evidence_type: str,
    text: str,
    keywords: list[str],
) -> bool:
    """Return whether one evidence item belongs to a diagnostic signal category."""
    category_type = ""
    for candidate in DIAGNOSTIC_SIGNAL_CANDIDATES:
        if candidate["category"] == category:
            category_type = str(candidate["evidence_type"])
            break
    return evidence_type == category_type or mentions_any(text, keywords)


def missing_tools_from_context(successful_tools: set[str], context: str) -> list[str]:
    """Return required but missing diagnostic tools from shared signal definitions."""
    missing: list[str] = []
    for tool_name in ["query_metrics", "query_logs"]:
        if tool_name not in successful_tools:
            missing.append(tool_name)

    for candidate in DIAGNOSTIC_SIGNAL_CANDIDATES[:4]:
        keywords = [str(item) for item in candidate["keywords"]]
        tools = [str(item) for item in candidate["tools"]]
        primary_tool = tools[0]
        if mentions_any(context, keywords) and primary_tool not in successful_tools:
            missing.append(primary_tool)
    return dedupe_strings(missing)


def is_metric_abnormal(output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    latency = output.get("p95_latency_ms") or {}
    error_rate = output.get("error_rate") or {}
    cpu = output.get("cpu") or {}
    memory = output.get("memory") or {}
    return any(
        [
            isinstance(latency, int | float) and latency >= 1000,
            isinstance(latency, dict) and latency.get("status") == "high",
            isinstance(error_rate, dict) and error_rate.get("status") == "high",
            alert_triggered(cpu),
            alert_triggered(memory),
        ]
    )


def is_redis_abnormal(output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    usage = output.get("client_usage_ratio")
    connected = output.get("connected_clients")
    maxclients = output.get("maxclients")
    if isinstance(usage, int | float) and usage >= 0.9:
        return True
    if isinstance(connected, int | float) and isinstance(maxclients, int | float) and maxclients:
        return connected / maxclients >= 0.9
    return alert_triggered(output)


def is_message_queue_abnormal(output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    signals = output.get("signals") or {}
    if not isinstance(signals, dict):
        return False
    lag_values = [
        signals.get("consumer_lag"),
        signals.get("max_partition_lag"),
        signals.get("lag"),
    ]
    if any(isinstance(value, int | float) and value > 0 for value in lag_values):
        return True
    under_replicated = signals.get("under_replicated_partitions")
    if isinstance(under_replicated, int | float) and under_replicated > 0:
        return True
    return signals.get("ready") is False


def is_message_queue_healthy(output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    signals = output.get("signals") or {}
    if not isinstance(signals, dict):
        return False
    lag_values = [
        signals.get("consumer_lag"),
        signals.get("max_partition_lag"),
        signals.get("lag"),
    ]
    lag_clear = all(not isinstance(value, int | float) or value <= 0 for value in lag_values)
    under_replicated = signals.get("under_replicated_partitions")
    replicas_clear = not isinstance(under_replicated, int | float) or under_replicated <= 0
    return signals.get("ready") is True and lag_clear and replicas_clear


def has_redis_exhaustion_signal(evidence_items: list[Any], joined_text: str) -> bool:
    for evidence in evidence_items:
        output = evidence_output(evidence)
        if not isinstance(output, dict):
            continue
        connected = output.get("connected_clients")
        maxclients = output.get("maxclients")
        usage = output.get("client_usage_ratio")
        triggered = output.get("alert_info", {}).get("triggered")
        if isinstance(usage, int | float) and usage >= 0.9:
            return True
        if (
            isinstance(connected, int | float)
            and isinstance(maxclients, int | float)
            and maxclients
        ):
            if connected / maxclients >= 0.9:
                return True
        if triggered and "maxclients" in str(output).lower():
            return True
    return "redis" in joined_text and "maxclients" in joined_text


def has_metrics_degradation_signal(evidence_items: list[Any], joined_text: str) -> bool:
    for evidence in evidence_items:
        output = evidence_output(evidence)
        if is_metric_abnormal(output):
            return True
    return mentions_any(joined_text, ["p95", "5xx", "error_rate", "错误率", "延迟"])


def has_log_timeout_signal(evidence_items: list[Any], joined_text: str) -> bool:
    if mentions_any(joined_text, ["timeout", "timed out", "5xx"]):
        return True
    return any(
        mentions_any(str(evidence_output(evidence)).lower(), ["timeout", "timed out", "5xx"])
        for evidence in evidence_items
    )


def has_mysql_signal(evidence_items: list[Any], joined_text: str) -> bool:
    for evidence in evidence_items:
        output = evidence_output(evidence)
        if isinstance(output, dict) and output.get("slow_queries"):
            return True
    return mentions_any(joined_text, ["mysql", "slow query", "慢查询", "lock_wait"])


def has_k8s_signal(evidence_items: list[Any], joined_text: str) -> bool:
    for evidence in evidence_items:
        output = evidence_output(evidence)
        if isinstance(output, dict) and mentions_any(
            str(output).lower(), ["crashloop", "notready", "restart"]
        ):
            return True
    return mentions_any(joined_text, ["crashloop", "pod not ready", "k8s", "kubernetes"])


def has_k8s_oom_signal(evidence_items: list[Any], context: str) -> bool:
    if mentions_any(context, ["oom", "oomkilled", "memory"]):
        return True
    return any(
        infer_evidence_type(str(item.get("source_tool") or "")) == "k8s"
        and mentions_any(str(evidence_output(item)).lower(), ["oom", "oomkilled"])
        for item in evidence_items
        if isinstance(item, dict)
    )


def has_message_queue_lag_signal(evidence_items: list[Any], joined_text: str) -> bool:
    for evidence in evidence_items:
        output = evidence_output(evidence)
        if not isinstance(output, dict):
            continue
        if is_message_queue_abnormal(output):
            return True
        if mentions_any(str(output).lower(), ["lagging", "consumer lag 高", "消息积压"]):
            return True
    return mentions_any(
        joined_text,
        ["redpanda", "kafka", "consumer lag", "max_partition_lag", "消息积压"],
    )


def alert_triggered(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    alert = value.get("alert_info")
    if isinstance(alert, dict) and alert.get("triggered"):
        return True
    return bool(value.get("triggered"))


def mentions_any(text: str, keywords: list[str]) -> bool:
    normalized = text.lower()
    return any(keyword.lower() in normalized for keyword in keywords)


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
