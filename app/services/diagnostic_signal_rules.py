"""Shared diagnostic signal rules for evidence and root-cause analysis."""

from __future__ import annotations

import re
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
        return str(DATA_SOURCE_ALIASES[normalized])
    if normalized == "mysql":
        origin = ""
        if isinstance(output, dict):
            origin = str(output.get("evidence_origin") or "")
        if origin == "mysql:aiops_service_catalog":
            return "cmdb"
        if origin == "mysql:aiops_deploy_history":
            return "deploy_history"
        if origin == "mysql:aiops_history_tickets":
            return "ticket_api"
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
        if is_metric_abnormal(output) or _metric_text_is_abnormal(text):
            return "supporting"
        if _metric_output_is_partial(output):
            return "neutral"
        if _metric_output_is_normal(output) or mentions_any(
            text,
            ["within threshold", "below threshold", "未超过阈值", "指标正常", "延迟正常"],
        ):
            return "refuting"
        return "unknown"
    if evidence_type == "log":
        if _log_output_is_empty(output) or mentions_any(
            text,
            ["0 条", "no error", "no matching log", "empty result", "未发现", "无异常日志"],
        ):
            return "refuting"
        observation_text = _log_observation_text(output, summary)
        if mentions_any(
            observation_text,
            ["error", "timeout", "5xx", "exception", "oom", "异常", "失败"],
        ):
            return "supporting"
        return "neutral"
    if evidence_type == "redis":
        if is_redis_abnormal(output) or mentions_any(
            text,
            ["连接数耗尽", "接近上限", "blocked_clients", "慢日志异常", "exhausted"],
        ):
            return "supporting"
        if _redis_output_is_normal(output):
            return "refuting"
        return "unknown"
    if evidence_type == "k8s":
        if mentions_any(text, ["crashloop", "oomkilled", "notready", "restart", "重启", "异常"]):
            return "supporting"
        if mentions_any(text, ["running", "未发现", "正常"]):
            return "refuting"
        return "neutral"
    if evidence_type == "mysql":
        if _mysql_output_is_abnormal(output) or _mysql_text_is_abnormal(text):
            return "supporting"
        if _mysql_output_is_normal(output):
            return "refuting"
        return "neutral"
    if evidence_type == "service_context":
        return (
            "supporting"
            if mentions_any(text, ["dependencies", "依赖", "owner", "namespace"])
            else "neutral"
        )
    if evidence_type == "change":
        if mentions_any(
            text,
            [
                "payment_report_enabled=true",
                "reconciliation report",
                "report feature",
                "feature flag",
                "slow query dashboard",
                "date-range query",
            ],
        ):
            return "supporting"
        return "neutral"
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
    if evidence_type in {"runbook", "ticket"}:
        return "neutral" if text.strip() else "unknown"
    if evidence_type == "risk":
        return "neutral"
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
        if stance == "supporting":
            return "指标阈值命中"
        if stance == "refuting":
            return "指标未命中异常阈值"
        return "指标数据不完整，无法判断异常或正常"
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
    if evidence_type == "service_context":
        return "服务依赖和责任人上下文已确认"
    if evidence_type == "change":
        return (
            "发布记录命中报表 Feature Flag 或查询路径变更，用于提高慢 SQL 假设排序"
            if stance == "supporting"
            else "发布记录仅提供时间线背景，不能单独证明根因"
        )
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
                str(item.get("summary", ""))
                for item in evidence_items
                if _is_usable_hypothesis_evidence(item)
            ),
            " ".join(
                str(evidence_output(item))
                for item in evidence_items
                if _is_usable_hypothesis_evidence(item)
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
                str(item.get("summary", ""))
                for item in evidence_items
                if _is_usable_hypothesis_evidence(item)
            ),
        ]
    ).lower()

    hypotheses: list[str] = []
    if has_redis_exhaustion_signal(evidence_items, joined_text):
        hypotheses.append(
            "Redis 连接数接近或达到 maxclients，导致应用侧 Redis connection timeout，并放大接口延迟和 5xx。"
        )
    if has_metrics_degradation_signal(evidence_items):
        hypotheses.append("服务 P95 延迟或 5xx 错误率异常升高，用户请求链路已受到影响。")
    if has_log_timeout_signal(evidence_items):
        hypotheses.append("错误日志出现 timeout 或下游缓存异常，可作为故障症状和调用链证据。")
    if has_mysql_signal(evidence_items, joined_text):
        hypotheses.append("MySQL 慢查询或连接池等待可能参与放大请求延迟。")
    if has_k8s_signal(evidence_items, joined_text):
        hypotheses.append("Kubernetes Pod 状态异常可能导致服务实例容量或稳定性下降。")
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
    auxiliary_types = {
        "redis_maxclients": {"log", "metric", "ticket", "runbook", "change"},
        "mysql_slow_query": {"log", "metric", "ticket", "runbook", "change"},
        "k8s_crashloop": {"log", "metric", "change"},
        "k8s_oomkilled": {"log", "metric"},
        "cpu_hot_loop": set(),
        "memory_leak": {"log", "k8s"},
        "disk_capacity": {"metric", "k8s"},
        "dependency_timeout": {"log"},
    }
    allowed_types = {category_type, *auxiliary_types.get(category, set())}
    return evidence_type in allowed_types and category_context_matches(category, text, keywords)


def category_context_matches(category: str, text: str, keywords: list[str]) -> bool:
    """Match a signal only when its category-specific condition is present."""
    normalized = text.lower()
    if category == "redis_maxclients":
        return mentions_any(
            normalized,
            [
                "maxclients",
                "connected_clients",
                "blocked_clients",
                "client_usage_ratio",
                "connection pool exhausted",
                "redis connection timeout",
                "连接数耗尽",
                "连接数接近上限",
            ],
        )
    if category == "mysql_slow_query":
        return mentions_any(
            normalized,
            ["slow query", "慢查询", "lock_wait", "锁等待", "pool_waiting", "sql digest"],
        )
    if category == "k8s_crashloop":
        return mentions_any(normalized, ["crashloop", "crash loop", "crashloopbackoff"])
    if category == "k8s_oomkilled":
        return _contains_oom_signal(normalized)
    if category == "cpu_hot_loop":
        return mentions_any(normalized, ["cpu", "load"]) and mentions_any(
            normalized,
            ["high", "spike", "hot", "saturation", "above threshold", "升高", "过高"],
        )
    if category == "memory_leak":
        return mentions_any(
            normalized,
            ["memory leak", "rss", "内存泄漏", "内存持续", "memory growth"],
        )
    if category == "disk_capacity":
        return mentions_any(
            normalized,
            ["disk full", "no space", "disk capacity", "磁盘空间", "磁盘耗尽", "写入失败"],
        )
    if category == "dependency_timeout":
        return mentions_any(
            normalized, ["dependency", "downstream", "下游", "依赖"]
        ) and mentions_any(
            normalized,
            ["timeout", "timed out", "unavailable", "5xx", "502", "503", "超时", "不可用"],
        )
    if category == "unknown_needs_human":
        return mentions_any(
            normalized,
            ["unclassified", "无法归类", "证据不足", "unaudited", "delete pod"],
        )
    return mentions_any(normalized, keywords)


def _matches_category_keywords(category: str, text: str, keywords: list[str]) -> bool:
    normalized = text.lower()
    if category == "redis_maxclients":
        return mentions_any(
            normalized,
            [
                "maxclients",
                "connected_clients",
                "blocked_clients",
                "client_usage_ratio",
                "connection pool exhausted",
                "连接数耗尽",
                "连接数接近上限",
            ],
        )
    if category == "mysql_slow_query":
        return mentions_any(
            normalized,
            ["slow query", "慢查询", "lock_wait", "锁等待", "pool_waiting", "sql digest"],
        )
    if category == "k8s_crashloop":
        if _contains_oom_signal(normalized) and "crashloop" not in normalized:
            return False
        return mentions_any(normalized, ["crashloop", "restart", "restarting", "重启"])
    if category == "k8s_oomkilled":
        return _contains_oom_signal(normalized)
    if category == "cpu_hot_loop":
        return mentions_any(normalized, ["cpu", "load"]) and mentions_any(
            normalized, ["high", "spike", "hot", "saturation", "above threshold"]
        )
    if category == "memory_leak":
        return mentions_any(
            normalized,
            ["memory leak", "rss", "内存泄漏", "内存持续", "memory growth"],
        )
    if category == "disk_capacity":
        return mentions_any(normalized, ["disk", "no space", "磁盘", "写入失败"])
    if category == "dependency_timeout":
        return mentions_any(
            normalized, ["dependency", "downstream", "下游", "依赖"]
        ) and mentions_any(
            normalized,
            ["timeout", "timed out", "unavailable", "5xx", "502", "503", "超时", "不可用"],
        )
    return mentions_any(normalized, keywords)


def missing_tools_from_context(successful_tools: set[str], context: str) -> list[str]:
    """Return required but missing diagnostic tools from shared signal definitions."""
    missing: list[str] = []
    for tool_name in ["query_metrics", "query_logs"]:
        if tool_name not in successful_tools:
            missing.append(tool_name)

    for candidate in DIAGNOSTIC_SIGNAL_CANDIDATES[:4]:
        category = str(candidate["category"])
        keywords = [str(item) for item in candidate["keywords"]]
        tools = [str(item) for item in candidate["tools"]]
        primary_tool = tools[0]
        if (
            category_context_matches(category, context, keywords)
            and primary_tool not in successful_tools
        ):
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
            _metric_value_is_high(latency, threshold=1000),
            isinstance(error_rate, int | float) and error_rate >= 0.01,
            _metric_value_is_high(error_rate, threshold=0.01),
            alert_triggered(cpu),
            alert_triggered(memory),
        ]
    )


def _metric_value_is_high(value: Any, *, threshold: float) -> bool:
    if not isinstance(value, dict):
        return False
    if str(value.get("status") or "").lower() == "high":
        return True
    current = value.get("current")
    return isinstance(current, int | float) and current >= threshold


def _metric_output_is_normal(output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    statuses: list[str] = []
    for key in ["p95_latency_ms", "error_rate", "cpu", "memory"]:
        value = output.get(key)
        if isinstance(value, dict) and value.get("status"):
            statuses.append(str(value["status"]).lower())
    return bool(statuses) and all(status in {"normal", "ok"} for status in statuses)


def _metric_output_is_partial(output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    if str(output.get("data_quality") or "").lower() == "partial":
        return True
    if output.get("empty_queries"):
        return True
    for key in ["qps", "p95_latency_ms", "error_rate", "cpu", "memory"]:
        value = output.get(key)
        if isinstance(value, dict) and str(value.get("status") or "").lower() in {
            "missing",
            "unavailable",
            "unknown",
        }:
            return True
    return False


def _metric_text_is_abnormal(text: str) -> bool:
    normalized = text.lower()
    if mentions_any(normalized, ["p95", "latency", "延迟"]) and mentions_any(
        normalized, ["high", "above threshold", "超过阈值", "升高"]
    ):
        return True
    if mentions_any(normalized, ["5xx", "error rate", "error_rate", "错误率"]) and mentions_any(
        normalized, ["high", "above threshold", "超过阈值", "升高"]
    ):
        return True

    latency_match = re.search(
        r"(?:p95(?:_latency_ms)?|latency)\s*[=:]\s*(\d+(?:\.\d+)?)\s*(ms|s)?",
        normalized,
    )
    if latency_match:
        latency = float(latency_match.group(1))
        if latency_match.group(2) == "s":
            latency *= 1000
        if latency >= 1000:
            return True

    error_match = re.search(
        r"(?:5xx|error[_ ]rate)\s*[=:]\s*(\d+(?:\.\d+)?)\s*(%)?",
        normalized,
    )
    if error_match:
        error_rate = float(error_match.group(1))
        threshold = 1.0 if error_match.group(2) else 0.01
        return error_rate >= threshold
    return False


def _log_output_is_empty(output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    logs = output.get("logs")
    if isinstance(logs, dict):
        total = logs.get("total")
        entries = logs.get("logs")
        if total == 0 or isinstance(entries, list) and not entries:
            return True
    signals = output.get("signals")
    return isinstance(signals, dict) and signals.get("log_count") == 0


def _log_observation_text(output: Any, summary: str) -> str:
    parts = [summary]
    if not isinstance(output, dict):
        parts.append(str(output))
        return " ".join(parts).lower()

    output_summary = output.get("summary")
    if output_summary and str(output_summary) != summary:
        parts.append(str(output_summary))
    logs = output.get("logs")
    if isinstance(logs, dict):
        entries = logs.get("logs")
        if isinstance(entries, list):
            parts.extend(str(item) for item in entries)
    elif isinstance(logs, list):
        parts.extend(str(item) for item in logs)
    return " ".join(parts).lower()


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


def _redis_output_is_normal(output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    usage = output.get("client_usage_ratio")
    connected = output.get("connected_clients")
    maxclients = output.get("maxclients")
    if isinstance(usage, int | float):
        return usage < 0.9
    if (
        isinstance(connected, int | float)
        and isinstance(maxclients, int | float)
        and maxclients > 0
    ):
        return connected / maxclients < 0.9
    return str(output.get("status") or "").lower() in {"normal", "ok", "healthy"}


def has_redis_exhaustion_signal(evidence_items: list[Any], joined_text: str) -> bool:
    for evidence in evidence_items:
        if not _is_usable_hypothesis_evidence(evidence):
            continue
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


def has_metrics_degradation_signal(evidence_items: list[Any]) -> bool:
    for evidence in evidence_items:
        if not isinstance(evidence, dict):
            continue
        if infer_evidence_type(str(evidence.get("source_tool") or "")) != "metric":
            continue
        if str(evidence.get("stance") or "") != "supporting":
            continue
        if is_metric_abnormal(evidence_output(evidence)):
            return True
    return False


def has_log_timeout_signal(evidence_items: list[Any]) -> bool:
    for evidence in evidence_items:
        if not isinstance(evidence, dict):
            continue
        if infer_evidence_type(str(evidence.get("source_tool") or "")) != "log":
            continue
        if str(evidence.get("stance") or "") != "supporting":
            continue
        output = evidence_output(evidence)
        text = _log_observation_text(output, str(evidence.get("summary") or ""))
        if mentions_any(text, ["timeout", "timed out", "5xx"]):
            return True
    return False


def has_mysql_signal(evidence_items: list[Any], joined_text: str) -> bool:
    for evidence in evidence_items:
        if not _is_usable_hypothesis_evidence(evidence):
            continue
        output = evidence_output(evidence)
        if not isinstance(output, dict):
            continue
        if _mysql_output_is_abnormal(output):
            return True
    return _mysql_text_is_abnormal(joined_text)


def _mysql_output_is_abnormal(output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    if output.get("slow_queries"):
        return True
    signals = output.get("signals")
    return isinstance(signals, dict) and any(
        _positive_number(signals.get(key))
        for key in ("slow_query_count", "pool_waiting", "lock_wait_count")
    )


def _mysql_output_is_normal(output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    slow_queries = output.get("slow_queries")
    signals = output.get("signals")
    observed: list[float] = []
    if isinstance(slow_queries, list):
        if slow_queries:
            return False
        observed.append(0)
    if isinstance(signals, dict):
        for key in ("slow_query_count", "pool_waiting", "lock_wait_count"):
            value = signals.get(key)
            if isinstance(value, int | float):
                observed.append(float(value))
    return bool(observed) and all(value == 0 for value in observed)


def _mysql_text_is_abnormal(text: str) -> bool:
    normalized = text.lower()
    if mentions_any(normalized, ["no slow query", "无慢查询", "未发现慢查询"]):
        return False
    if mentions_any(normalized, ["slow query", "慢查询", "sql digest"]):
        return True
    return _positive_named_signal(
        normalized,
        ["slow_query_count", "pool_waiting", "lock_wait", "lock_wait_count"],
    )


def _positive_named_signal(text: str, names: list[str]) -> bool:
    names_pattern = "|".join(re.escape(name) for name in names)
    for match in re.finditer(
        rf"(?:{names_pattern})[^0-9]{{0,8}}(\d+(?:\.\d+)?)",
        text,
    ):
        if float(match.group(1)) > 0:
            return True
    return False


def _positive_number(value: Any) -> bool:
    return isinstance(value, int | float) and value > 0


def has_k8s_signal(evidence_items: list[Any], joined_text: str) -> bool:
    for evidence in evidence_items:
        if not _is_usable_hypothesis_evidence(evidence):
            continue
        output = evidence_output(evidence)
        if isinstance(output, dict) and mentions_any(
            str(output).lower(), ["crashloop", "notready", "restart"]
        ):
            return True
    return mentions_any(joined_text, ["crashloop", "pod not ready", "k8s", "kubernetes"])


def has_k8s_oom_signal(evidence_items: list[Any], context: str) -> bool:
    if _contains_oom_signal(context):
        return True
    return any(
        infer_evidence_type(str(item.get("source_tool") or "")) == "k8s"
        and _contains_oom_signal(str(evidence_output(item)).lower())
        for item in evidence_items
        if _is_usable_hypothesis_evidence(item)
    )


def _is_usable_hypothesis_evidence(evidence: Any) -> bool:
    if not isinstance(evidence, dict):
        return False
    raw_data = evidence.get("raw_data") or {}
    if isinstance(raw_data, dict) and str(raw_data.get("status") or "").lower() == "failed":
        return False
    metadata = raw_data.get("metadata") if isinstance(raw_data, dict) else {}
    quality = metadata.get("evidence_quality") if isinstance(metadata, dict) else {}
    return not isinstance(quality, dict) or quality.get("usable", True) is not False


def _contains_oom_signal(text: str) -> bool:
    normalized = text.lower()
    return bool(
        re.search(r"\boom(?:killed)?\b", normalized)
        or mentions_any(
            normalized,
            ["out of memory", "memory limit exceeded", "内存溢出", "超过内存限制"],
        )
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
