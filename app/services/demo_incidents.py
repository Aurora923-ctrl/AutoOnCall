"""Central demo incident catalog for AIOps interview and local demos."""

from __future__ import annotations

from typing import Any

from app.models.incident import Incident

DEMO_INCIDENT_ORDER = [
    "redis_maxclients",
    "mysql_slow_query",
    "pod_crashloop",
    "forbidden_sql",
]

DEMO_INCIDENT_ALIASES = {
    "redis-maxclients": "redis_maxclients",
    "mysql-slow-query": "mysql_slow_query",
    "k8s-crashloop": "pod_crashloop",
}

DEMO_INCIDENT_LABELS = {
    "redis_maxclients": "Redis maxclients",
    "mysql_slow_query": "MySQL slow query",
    "pod_crashloop": "Pod CrashLoop",
    "forbidden_sql": "Forbidden SQL",
}

DEMO_INCIDENTS: dict[str, dict[str, Any]] = {
    "redis_maxclients": {
        "incident_id": "INC-REDIS-001",
        "title": "order-service Redis maxclients exhausted",
        "service_name": "order-service",
        "severity": "P1",
        "symptom": "订单服务大量 503，P95 延迟超过 3 秒，日志出现 Redis connection timeout，怀疑 maxclients 耗尽",
        "environment": "prod",
        "raw_alert": {
            "alertname": "RedisMaxClientsNearLimit",
            "dependency": "redis-order",
            "connected_clients": 9940,
            "maxclients": 10000,
            "blocked_clients": 37,
            "requested_action": "apply_config_change",
            "reason": "调整 Redis maxclients；如应用连接未恢复，再由人工评估重启 order-service",
            "config_key": "maxclients",
            "config_value": 12000,
        },
    },
    "mysql_slow_query": {
        "incident_id": "INC-MYSQL-001",
        "title": "payment-service MySQL slow query latency",
        "service_name": "payment-service",
        "severity": "P2",
        "symptom": "支付接口 P95 升高但错误率仅小幅上升，日志出现慢 SQL digest 和连接池等待，近期刚开启报表 Feature Flag",
        "environment": "prod",
        "raw_alert": {
            "alertname": "MySQLSlowQueryLatency",
            "dependency": "payment-mysql",
            "mysql_instance": "payment-mysql",
            "slow_query_count": 18,
            "pool_waiting": 6,
            "active_connections": 188,
            "max_connections": 200,
            "sql_digest": "9f3a-pay-report",
            "recent_change_id": "CHG-10087",
            "feature_flag": "PAYMENT_REPORT_ENABLED=true",
        },
    },
    "pod_crashloop": {
        "incident_id": "INC-K8S-001",
        "title": "inventory-service Kubernetes pod crash loop",
        "service_name": "inventory-service",
        "severity": "P1",
        "symptom": "库存服务 Pod 持续 CrashLoopBackOff，实例容量下降，接口偶发 503",
        "environment": "prod",
        "raw_alert": {
            "alertname": "PodCrashLoopBackOff",
            "namespace": "inventory",
            "pod": "inventory-service-7f8d9c-abc12",
            "restarts": 12,
        },
    },
    "forbidden_sql": {
        "incident_id": "INC-SQL-001",
        "title": "order-service forbidden unaudited SQL",
        "service_name": "order-service",
        "severity": "P1",
        "symptom": "需要立即执行未审核 SQL 清理异常订单数据",
        "environment": "prod",
        "raw_alert": {
            "requested_action": "execute_sql",
            "sql": "DELETE FROM orders WHERE status = 'abnormal';",
            "audited": False,
            "reason": "业务方要求立刻清理异常订单",
        },
    },
}


class DemoIncidentNotFoundError(KeyError):
    """Raised when a requested demo incident case does not exist."""


def list_demo_incident_items() -> list[dict[str, Any]]:
    """Return frontend-ready demo incident catalog items."""
    items = []
    for case_id in DEMO_INCIDENT_ORDER:
        incident = build_demo_incident(case_id)
        aliases = [
            alias
            for alias, canonical_id in DEMO_INCIDENT_ALIASES.items()
            if canonical_id == case_id
        ]
        items.append(
            {
                "case_id": case_id,
                "label": DEMO_INCIDENT_LABELS.get(case_id, case_id),
                "aliases": aliases,
                "incident": incident.model_dump(mode="json"),
            }
        )
    return items


def build_demo_incident(case_id: str) -> Incident:
    """Build an Incident model for a demo case id or alias."""
    canonical_id = canonical_demo_case_id(case_id)
    payload = DEMO_INCIDENTS.get(canonical_id)
    if payload is None:
        raise DemoIncidentNotFoundError(case_id)
    return Incident(**payload)


def canonical_demo_case_id(case_id: str) -> str:
    """Normalize a demo case alias to its canonical id."""
    return DEMO_INCIDENT_ALIASES.get(case_id, case_id)


def demo_incident_aliases(case_id: str) -> list[str]:
    """Return aliases for one canonical demo case."""
    canonical_id = canonical_demo_case_id(case_id)
    return [
        alias
        for alias, alias_canonical_id in DEMO_INCIDENT_ALIASES.items()
        if alias_canonical_id == canonical_id
    ]


def available_demo_case_ids() -> list[str]:
    """Return the ordered list of supported demo case ids."""
    return list(DEMO_INCIDENT_ORDER)
