"""Query intent inference and table-driven retrieval preferences."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def infer_retrieval_preferences(query: str) -> dict[str, set[str] | bool]:
    """Infer conservative source preferences from explicit query wording."""
    lowered = str(query or "").lower()
    preferred_doc_types: set[str] = set()
    preferred_extensions: set[str] = set()
    preferred_heading_terms: set[str] = set()
    required_sources: set[str] = set()

    if any(
        term in lowered for term in {"postmortem", "复盘", "事故复盘", "历史事故", "事故时间线"}
    ):
        preferred_doc_types.add("pdf")
        preferred_extensions.add(".pdf")
    if any(term in lowered for term in {"incident-window", "事故窗口", "故障窗口"}):
        preferred_doc_types.add("pdf")
        preferred_extensions.add(".pdf")
    if any(
        term in lowered for term in {"wiki", "runbook", "知识库", "如何排查", "应先查", "先查什么"}
    ):
        preferred_doc_types.add("html")
        preferred_extensions.update({".html", ".htm"})
    if any(term in lowered for term in {"审批边界", "容量边界"}):
        preferred_doc_types.add("html")
        preferred_extensions.update({".html", ".htm"})
    if "redis" in lowered and any(term in lowered for term in {"两个可信来源", "哪两个"}):
        preferred_doc_types.update({"pdf", "html"})
        preferred_extensions.update({".pdf", ".html", ".htm"})
    if any(
        term in lowered
        for term in {
            "ticket",
            "工单",
            "历史记录",
            "历史案例",
            "inc-",
            "deploy_history",
            "部署历史",
            "版本记录",
        }
    ):
        preferred_doc_types.add("table")
    explicit_xlsx_history = (
        "tickets.xlsx" in lowered
        or "deploy_history" in lowered
        or "部署历史" in lowered
        or "版本记录" in lowered
        or bool(re.search(r"\b[a-z][a-z0-9-]*-\d{4}\.\d{2}\.\d{2}-rc\d+\b", lowered))
    )
    if "inc-" in lowered and not explicit_xlsx_history:
        # Incident IDs are present in both legacy CSV exports and the current
        # workbook. Keep both sources eligible instead of requiring one exact
        # filename, which would reject a valid deployment using the other form.
        preferred_extensions.update({".csv", ".xlsx"})
    if (
        "tickets.xlsx" in lowered
        or "deploy_history" in lowered
        or "部署历史" in lowered
        or "版本记录" in lowered
        or re.search(r"\brc\d+\b", lowered)
    ):
        preferred_extensions.update({".csv", ".xlsx"})
        if (
            "tickets.xlsx" in lowered
            or "deploy_history" in lowered
            or "部署历史" in lowered
            or "版本记录" in lowered
            or re.search(r"\b[a-z][a-z0-9-]*-\d{4}\.\d{2}\.\d{2}-rc\d+\b", lowered)
        ):
            required_sources.add("tickets.xlsx")
    if explicit_xlsx_history:
        required_sources.discard("tickets.csv")

    if any(term in lowered for term in {"证据", "取证", "如何收集", "怎样区分"}):
        preferred_heading_terms.update({"排查步骤", "常用命令", "相关工具命令"})
    if any(term in lowered for term in {"先确认", "哪些信号", "排查步骤"}):
        preferred_heading_terms.add("排查步骤")
    if any(
        term in lowered
        for term in {
            "处置边界",
            "审批",
            "重启",
            "扩容",
            "限流",
            "回滚",
            "删除",
            "截断",
            "清理",
            "dry-run",
        }
    ):
        preferred_heading_terms.add("升级与审批")
    if (
        "redis" in lowered
        and "官方" in lowered
        and any(term in lowered for term in {"复盘", "事故"})
    ):
        required_sources.update({"official_redis_clients.md", "redis_postmortem.pdf"})
    if (
        "redis" in lowered
        and "maxclients" in lowered
        and any(term in lowered for term in {"capacity wiki", "容量 wiki", "容量wiki"})
    ):
        required_sources.update({"official_redis_clients.md", "redis_capacity_wiki.html"})
    if (
        "mysql" in lowered
        and any(term in lowered for term in {"runbook", "支付", "payment"})
        and any(term in lowered for term in {"复盘", "postmortem", "事故"})
    ):
        required_sources.update({"payment_wiki.html", "mysql_slow_query_postmortem.pdf"})
    if (
        any(term in lowered for term in {"kubernetes", "k8s"})
        and "pod" in lowered
        and "service" in lowered
        and "endpointslice" in lowered
    ):
        required_sources.update(
            {
                "official_kubernetes_debug_pods.md",
                "official_kubernetes_debug_services.md",
            }
        )
    if (
        "loki" in lowered
        and "discarded" in lowered
        and any(term in lowered for term in {"告警", "alert"})
    ):
        required_sources.update(
            {
                "official_loki_troubleshoot_ingest.md",
                "official_prometheus_alerting_practices.md",
            }
        )
    if (
        any(term in lowered for term in {"kubernetes", "k8s"})
        and "pod" in lowered
        and "service" in lowered
        and any(term in lowered for term in {"同时", "联合", "哪些官方文档"})
    ):
        required_sources.update(
            {
                "official_kubernetes_debug_pods.md",
                "official_kubernetes_debug_services.md",
            }
        )
    if (
        any(term in lowered for term in {"loki", "日志", "可观测性"})
        and any(term in lowered for term in {"ingestion", "摄取", "写入"})
        and any(term in lowered for term in {"prometheus", "告警", "指标"})
    ):
        required_sources.update(
            {
                "official_loki_troubleshoot_ingest.md",
                "official_prometheus_alerting_practices.md",
            }
        )

    specific_fault_query = any(
        term in lowered
        for term in {
            "redis",
            "mysql",
            "sql",
            "maxclients",
            "pool_waiting",
            "active_connections",
            "retry",
            "重试",
        }
    )
    generic_service_query = any(
        term in lowered
        for term in {
            "503",
            "5xx",
            "service unavailable",
            "服务不可用",
            "接口失败",
            "请求失败",
        }
    )
    dependency_outage_query = (
        any(term in lowered for term in {"依赖", "下游"})
        and any(term in lowered for term in {"不可用", "接口失败", "请求失败", "超时"})
    )
    generic_slow_response_query = any(
        term in lowered for term in {"接口响应慢", "接口变慢", "响应时间增加", "响应延迟"}
    )
    if dependency_outage_query:
        required_sources.add("service_unavailable.md")
    if generic_slow_response_query:
        required_sources.add("slow_response.md")
    preferred_source_terms = {
        term
        for term, aliases in {
            "redis": {
                "redis",
                "maxclients",
                "blocked_clients",
                "connected_clients",
                "空闲客户端",
                "客户端连接",
                "服务端关闭",
                "连接槽位",
            },
            "mysql": {
                "mysql",
                "sql",
                "pool_waiting",
                "active_connections",
                "慢查询",
                "慢 sql",
                "慢sql",
                "连接池",
                "数据库",
                "支付",
                "payment",
            },
            "kubernetes": {
                "kubernetes",
                "k8s",
                "pod",
                "service",
                "endpointslice",
                "clusterip",
                "容器",
            },
            "prometheus": {
                "prometheus",
                "promql",
                "alerting",
                "告警规则",
                "用户可见",
                "告警原则",
                "内部原因",
                "pending",
                "firing",
                "症状告警",
                "告警实践",
            },
            "loki": {
                "loki",
                "logql",
                "ingestion",
                "ingester",
                "日志查询",
                "日志摄取",
                "日志查询语言",
                "返回 400",
                "可观测性写入",
            },
            "cpu": {"cpu", "load", "线程", "火焰图"},
            "memory": {"memory", "oom", "oomkilled", "内存"},
            "disk": {"disk", "inode", "no space", "磁盘"},
            "service_unavailable": {"503", "5xx", "服务不可用", "无法访问", "接口全部失败"},
            "slow_response": {
                "slow",
                "latency",
                "p95",
                "响应延迟",
                "响应时间",
                "接口变慢",
                "外部接口",
                "下游",
            },
        }.items()
        if any(alias in lowered for alias in aliases)
    }
    dominant_source_terms = {
        term
        for term, aliases in {
            "redis": {
                "maxclients",
                "blocked_clients",
                "connected_clients",
                "空闲客户端",
                "客户端连接",
                "服务端关闭",
                "连接上限",
                "连接槽位",
                "连接数满",
                "新连接被拒绝",
                "客户端数",
                "客户端限制",
                "缓存节点",
            },
            "mysql": {
                "mysql",
                "sql",
                "pool_waiting",
                "active_connections",
                "慢查询",
                "慢 sql",
                "慢sql",
                "连接池",
                "数据库",
                "支付",
                "payment",
            },
            "kubernetes": {"kubernetes", "k8s", "endpointslice", "clusterip", "selector", "pod"},
            "prometheus": {
                "prometheus",
                "promql",
                "alerting rule",
                "告警规则",
                "用户可见",
                "告警原则",
                "内部原因",
                "pending",
                "firing",
                "症状告警",
                "告警实践",
            },
            "loki": {
                "loki",
                "logql",
                "ingestion",
                "ingester",
                "日志摄取",
                "日志查询语言",
                "返回 400",
                "可观测性写入",
            },
            "cpu": {"cpu", "load", "火焰图"},
            "memory": {"memory", "oom", "oomkilled", "内存"},
            "disk": {"disk", "inode", "no space", "磁盘"},
            "service_unavailable": {"503", "5xx", "服务不可用", "无法访问", "接口全部失败"},
            "slow_response": {"响应延迟", "响应时间", "接口变慢", "外部接口", "下游"},
        }.items()
        if any(alias in lowered for alias in aliases)
    }
    explicit_mysql_evidence = any(
        term in lowered
        for term in {
            "mysql",
            "pool_waiting",
            "active_connections",
            "数据库",
            "连接池",
            "payment",
        }
    )
    mixed_cpu_sql_signal = "cpu" in lowered and "sql" in lowered and not explicit_mysql_evidence
    if (
        "mysql" in dominant_source_terms
        or any(term in lowered for term in {"慢 sql", "慢sql", "pool_waiting", "数据库"})
    ) and not mixed_cpu_sql_signal:
        dominant_source_terms.discard("cpu")
    if dominant_source_terms & {"cpu", "memory", "disk"}:
        dominant_source_terms.discard("kubernetes")
    if {"redis", "mysql", "kubernetes", "prometheus", "loki"} & dominant_source_terms:
        overshadowed_generic_terms = {"memory", "disk", "service_unavailable"}
        if not mixed_cpu_sql_signal:
            overshadowed_generic_terms.add("cpu")
        dominant_source_terms.difference_update(overshadowed_generic_terms)
    if dependency_outage_query:
        dominant_source_terms = {"service_unavailable"}
    elif generic_slow_response_query:
        dominant_source_terms.add("slow_response")
    return {
        "preferred_doc_types": preferred_doc_types,
        "preferred_extensions": preferred_extensions,
        "preferred_source_terms": preferred_source_terms,
        "dominant_source_terms": dominant_source_terms,
        "preferred_heading_terms": preferred_heading_terms,
        "required_sources": required_sources,
        "penalize_generic_service": specific_fault_query and not generic_service_query,
        "require_source_diversity": any(
            term in lowered
            for term in {
                "同时",
                "结合",
                "两个",
                "哪两",
                "多来源",
                "多格式",
                "分别",
                "联合",
                "相互印证",
                "交叉验证",
                "转化为",
            }
        ),
        "prefer_ticket_history": "inc-" in lowered
        or any(
            term in lowered
            for term in {
                "ticket",
                "工单",
                "历史记录",
                "历史案例",
                "deploy_history",
                "部署历史",
                "版本记录",
            }
        ),
        "prefer_service_debug": any(
            term in lowered
            for term in {
                "endpointslice",
                "endpoints",
                "selector",
                "clusterip",
                "service 与 pod",
                "service 后端",
            }
        ),
        "prefer_service_backend": any(
            term in lowered
            for term in {
                "endpointslice",
                "endpoints",
                "selector",
                "clusterip",
                "后端地址",
                "service 后端",
            }
        ),
        "prefer_redis_clients": "redis" in lowered
        and any(
            term in lowered for term in {"maxclients", "connected_clients", "连接数", "客户端"}
        ),
        "prefer_error_definition": "400" in lowered,
    }


def query_has_oncall_scope(query: str) -> bool:
    """Return whether the query explicitly contains an operations or incident signal."""
    lowered = str(query or "").lower()
    return any(
        marker in lowered
        for marker in {
            "cpu",
            "memory",
            "oom",
            "oomkilled",
            "jvm",
            "gc",
            "disk",
            "inode",
            "redis",
            "mysql",
            "sql",
            "mq",
            "503",
            "5xx",
            "timeout",
            "latency",
            "p95",
            "kubernetes",
            "k8s",
            "pod",
            "service",
            "endpointslice",
            "loki",
            "prometheus",
            "promql",
            "logql",
            "告警",
            "故障",
            "异常",
            "不可用",
            "超时",
            "慢查询",
            "响应慢",
            "响应延迟",
            "变慢",
            "连接池",
            "缓存",
            "日志",
            "指标",
            "排查",
            "取证",
            "复盘",
            "恢复",
            "健康检查",
            "错误率",
            "工单",
            "发布",
            "启动失败",
            "配置文件",
            "环境变量",
            "回滚",
            "扩容",
            "限流",
            "重启",
        }
    )


def retrieval_intent_multiplier(
    chunk: dict[str, Any],
    preferences: dict[str, set[str] | bool],
) -> float:
    """Return a small ranking multiplier without overriding base relevance."""
    source_file = str(chunk.get("source_file") or "")
    metadata = dict(chunk.get("metadata") or {})
    suffix = Path(source_file).suffix.lower()
    doc_type = str(metadata.get("doc_type") or "").strip().lower()
    if not doc_type:
        doc_type = _doc_type_from_suffix(suffix)

    raw_doc_types = preferences.get("preferred_doc_types")
    raw_extensions = preferences.get("preferred_extensions")
    preferred_doc_types = set(raw_doc_types) if isinstance(raw_doc_types, set) else set()
    preferred_extensions = set(raw_extensions) if isinstance(raw_extensions, set) else set()
    raw_source_terms = preferences.get("preferred_source_terms")
    preferred_source_terms = set(raw_source_terms) if isinstance(raw_source_terms, set) else set()
    raw_dominant_terms = preferences.get("dominant_source_terms")
    dominant_source_terms = (
        set(raw_dominant_terms) if isinstance(raw_dominant_terms, set) else set()
    )
    raw_heading_terms = preferences.get("preferred_heading_terms")
    preferred_heading_terms = (
        set(raw_heading_terms) if isinstance(raw_heading_terms, set) else set()
    )
    raw_required_sources = preferences.get("required_sources")
    required_sources = set(raw_required_sources) if isinstance(raw_required_sources, set) else set()
    normalized_source = source_file.lower().replace("-", "_")
    normalized_heading = str(chunk.get("heading_path") or "").lower()
    multiplier = 1.0
    if suffix and suffix in preferred_extensions:
        multiplier *= 1.22
    elif preferred_extensions:
        multiplier *= 0.88
    if doc_type and doc_type in preferred_doc_types:
        multiplier *= 1.12
    elif preferred_doc_types:
        multiplier *= 0.92
    if bool(preferences.get("prefer_ticket_history")):
        if doc_type == "table" or suffix in {".csv", ".xlsx", ".xls", ".tsv"}:
            multiplier *= 1.8
        elif "tickets" in normalized_source:
            multiplier *= 1.8
    if bool(preferences.get("penalize_generic_service")) and source_file.lower() == (
        "service_unavailable.md"
    ):
        multiplier *= 0.78
    if bool(preferences.get("prefer_service_debug")):
        if "debug_services" in normalized_source or "debug_pods" in normalized_source:
            multiplier *= 1.6
    if bool(preferences.get("prefer_service_backend")):
        if "debug_services" in normalized_source:
            multiplier *= 1.8
        elif "debug_pods" in normalized_source:
            multiplier *= 0.72
    if bool(preferences.get("prefer_redis_clients")):
        if normalized_source == "official_redis_clients.md":
            multiplier *= 1.6
        elif normalized_source == "official_redis_latency.md":
            multiplier *= 0.35
    if source_file.lower() in required_sources:
        multiplier *= 1.65
    if preferred_heading_terms:
        if any(term.lower() in normalized_heading for term in preferred_heading_terms):
            multiplier *= 1.55
        elif any(
            term in normalized_heading
            for term in {"告警名称", "问题描述", "相关告警", "预防措施", "长期优化"}
        ):
            multiplier *= 0.68
    searchable_text = " ".join(
        [
            normalized_source,
            str(chunk.get("heading_path") or "").lower(),
            str(chunk.get("content") or "").lower(),
        ]
    )
    if bool(preferences.get("prefer_error_definition")) and "400 bad request" in searchable_text:
        multiplier *= 1.35
    if dominant_source_terms:
        source_matches = sum(term in normalized_source for term in dominant_source_terms)
        content_matches = sum(term in searchable_text for term in dominant_source_terms)
        if source_matches:
            multiplier *= 2.4
        elif content_matches:
            multiplier *= 1.35
        elif source_file.lower() in {
            "cpu_high_usage.md",
            "memory_high_usage.md",
            "disk_high_usage.md",
            "service_unavailable.md",
            "slow_response.md",
        }:
            multiplier *= 0.55
    elif preferred_source_terms:
        if any(term in normalized_source for term in preferred_source_terms):
            multiplier *= 1.45
        elif source_file.lower() in {
            "cpu_high_usage.md",
            "memory_high_usage.md",
            "disk_high_usage.md",
            "service_unavailable.md",
            "slow_response.md",
        }:
            multiplier *= 0.78
    return multiplier


def _required_sources_from_preferences(
    preferences: dict[str, set[str] | bool],
) -> set[str]:
    raw_required_sources = preferences.get("required_sources")
    return set(raw_required_sources) if isinstance(raw_required_sources, set) else set()


def _doc_type_from_suffix(suffix: str) -> str:
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".csv", ".xlsx", ".xls", ".tsv"}:
        return "table"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    return suffix.lstrip(".")


def extract_exact_retrieval_entities(query: str) -> set[str]:
    """Extract stable identifiers whose spelling must survive semantic retrieval."""
    text = str(query or "")
    composite_patterns = (
        r"\bINC-[A-Z0-9]+-\d+\b",
        r"\b[a-z][a-z0-9-]*-\d{4}\.\d{2}\.\d{2}-rc\d+\b",
        r"\b[a-z][a-z0-9_-]*(?:_id)?=[A-Z0-9][A-Z0-9._:-]+\b",
    )
    entities = {
        match.group(0).casefold()
        for pattern in composite_patterns
        for match in re.finditer(pattern, text, flags=re.IGNORECASE)
    }
    masked = text
    for entity in sorted(entities, key=len, reverse=True):
        masked = re.sub(re.escape(entity), " ", masked, flags=re.IGNORECASE)
    entities.update(
        match.group(0).casefold()
        for match in re.finditer(r"\b(?:rc|v)\d+(?:\.\d+){0,3}\b", masked, flags=re.IGNORECASE)
    )
    return entities


def build_targeted_lexical_queries(query: str) -> dict[str, str]:
    """Build conservative query expansions only for explicit retrieval subgoals."""
    lowered = str(query or "").lower()
    preferences = infer_retrieval_preferences(query)
    targeted: dict[str, str] = {}
    required_sources = _required_sources_from_preferences(preferences)
    source_hints = {
        "official_redis_clients.md": "maxclients maximum concurrent connected clients accepting connections",
        "redis_postmortem.pdf": "incident window connected_clients blocked_clients maxclients postmortem",
        "official_kubernetes_debug_pods.md": "debug pods kubectl describe pod state events running readiness",
        "official_kubernetes_debug_services.md": "debug service selector endpointslice backend pods",
        "official_loki_troubleshoot_ingest.md": (
            "loki discarded samples bytes monitoring ingestion errors"
        ),
        "official_prometheus_alerting_practices.md": (
            "alert symptoms user-visible pain what to alert on"
        ),
        "redis_capacity_wiki.html": (
            "redis capacity wiki maxclients incident-window live_info approval boundary"
        ),
        "payment_wiki.html": "payment runbook mysql slow query explain pool_waiting",
        "mysql_slow_query_postmortem.pdf": (
            "mysql slow query postmortem active_connections pool_waiting incident window"
        ),
        "tickets.xlsx": "deploy_history release version change_summary rollback",
    }
    for source_file in required_sources:
        targeted[source_file] = f"{query} {source_hints.get(source_file, source_file)}"

    runbook_source = next(
        (
            source
            for term, source in {
                "cpu": "cpu_high_usage.md",
                "oom": "memory_high_usage.md",
                "oomkilled": "memory_high_usage.md",
                "鍐呭瓨": "memory_high_usage.md",
                "inode": "disk_high_usage.md",
                "纾佺洏": "disk_high_usage.md",
            }.items()
            if term in lowered
        ),
        "",
    )
    raw_heading_terms = preferences.get("preferred_heading_terms")
    heading_terms = set(raw_heading_terms) if isinstance(raw_heading_terms, set) else set()
    if runbook_source and heading_terms:
        targeted[runbook_source] = f"{query} {' '.join(sorted(heading_terms))}"
    return targeted
