"""Static diagnostic signal catalog shared by evidence analysis."""

from __future__ import annotations

from typing import Any

KNOWN_EVIDENCE_SOURCES = {
    "prometheus",
    "loki",
    "log_gateway",
    "cmdb",
    "deploy_history",
    "redis_info",
    "kubernetes",
    "mysql",
    "ticket_api",
    "mcp_monitor",
    "mcp_monitor_mixed",
    "mcp_cls",
    "eval_fixture",
    "mock",
    "rule_based",
    "rag",
}

DATA_SOURCE_ALIASES = {
    "metrics": "not_configured",
    "logs": "not_configured",
}

FALLBACK_EXECUTION_PATHS = {"manual_analysis", "llm_toolnode_fallback"}

DIAGNOSTIC_SIGNAL_CANDIDATES: list[dict[str, Any]] = [
    {
        "category": "redis_maxclients",
        "title": "Redis maxclients 或连接池耗尽导致 timeout 和 5xx。",
        "keywords": [
            "redis",
            "cache",
            "maxclients",
            "connected_clients",
            "connection timeout",
            "连接数",
        ],
        "tools": ["query_redis_status", "query_metrics", "query_logs"],
        "evidence_type": "redis",
    },
    {
        "category": "mysql_slow_query",
        "title": "MySQL 慢查询、连接池等待或锁等待放大接口延迟。",
        "keywords": ["mysql", "sql", "slow query", "慢查询", "lock_wait", "锁等待", "pool_waiting"],
        "tools": ["query_mysql_status", "query_metrics", "query_logs"],
        "evidence_type": "mysql",
    },
    {
        "category": "pod_crashloop",
        "title": "Kubernetes Pod CrashLoopBackOff 或频繁重启导致实例容量下降。",
        "keywords": ["crashloop", "pod", "k8s", "kubernetes", "restart", "oomkilled", "重启"],
        "tools": ["query_k8s_status", "query_logs", "query_metrics"],
        "evidence_type": "k8s",
    },
    {
        "category": "cpu_high",
        "title": "CPU 使用率过高导致请求排队、P95 升高或错误率上升。",
        "keywords": ["cpu", "load", "使用率"],
        "tools": ["query_metrics", "query_logs"],
        "evidence_type": "metric",
    },
    {
        "category": "memory_oom",
        "title": "内存压力或 OOMKilled 导致服务不稳定。",
        "keywords": ["memory", "oom", "oomkilled", "内存"],
        "tools": ["query_metrics", "query_logs", "query_k8s_status"],
        "evidence_type": "metric",
    },
    {
        "category": "disk_full",
        "title": "磁盘空间耗尽或写入失败导致服务异常。",
        "keywords": ["disk", "no space", "磁盘", "写入失败"],
        "tools": ["query_metrics", "query_logs", "query_k8s_status"],
        "evidence_type": "log",
    },
    {
        "category": "service_5xx",
        "title": "服务 5xx 或 P95 异常升高，用户请求链路受影响。",
        "keywords": ["5xx", "503", "502", "p95", "unavailable", "不可用", "延迟"],
        "tools": ["query_metrics", "query_logs"],
        "evidence_type": "metric",
    },
]
