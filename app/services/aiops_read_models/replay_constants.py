"""Constants used by incident replay read models."""

from __future__ import annotations

DEMO_INCIDENT_EVAL_CASE_IDS = {
    "inc-redis-001": "redis_maxclients_timeout",
    "inc-mysql-001": "mysql_slow_query_latency",
    "inc-k8s-001": "pod_crashloop",
    "inc-sql-001": "forbidden_unaudited_sql",
}
