# Redis Maxclients Golden Chain

## 5-Minute Walkthrough

Redis is a live adapter-backed golden chain. The current container runtime proves the adapter is connected; the outage-window facts come from the Redis key `autooncall:incident:order-service:redis-maxclients`.

1. Start from the Alertmanager payload: `RedisMaxClientsHigh` for `order-service`, `redis_instance=redis-cluster-prod`, severity `critical`.
2. Incident fields normalize to `service_name=order-service`, `severity=P1`, `environment=prod`, symptom `Redis connection timeout and 5xx spike`.
3. Planner first checks Redis state, then symptoms through Prometheus and Loki, then Runbook/history for remediation context.
4. Evidence shows `connected_clients=9940/maxclients=10000` in incident evidence, elevated 5xx/P95 metrics, timeout logs, and a related historical ticket.
5. Root cause is Redis connection exhaustion causing application connection-pool wait and request timeouts.
6. Diagnosis is read-only and does not need approval; changing Redis maxclients, restarting Redis, or changing app pool settings requires approval.

## Fixed Chain Contract

- Alertmanager payload: `eval/cases.yaml#redis_maxclients_timeout`.
- Incident fields: `order-service`, `P1`, `prod`, Redis timeout and 5xx symptom.
- Planner expected steps: `query_redis_status -> query_metrics -> query_logs -> search_runbook -> search_history_ticket`.
- Actual tool order requirement: Redis status before metrics/logs.
- Evidence fields: every evidence item must expose `fact`, `inference`, and `uncertainty`.
- Root cause: Redis connected clients reached maxclients and application connection pool timed out.
- Remediation: reduce traffic/retries, inspect hot keys, tune connection pools, increase Redis capacity or maxclients only through approved change.
- Approval: diagnosis no; remediation change yes.
- Report must contain: Redis evidence timeline, runtime vs incident boundary, connected clients/maxclients, metrics/log symptom evidence, approval boundary.
- Eval case: `redis_maxclients_timeout`.

## Evidence Checklist

| Evidence | Fact | Inference | Uncertainty |
| --- | --- | --- | --- |
| Redis incident key | `connected_clients=9940/maxclients=10000` | Redis accepted connections were near the configured ceiling | Current `live_info` may be idle; this is replay-window evidence |
| Prometheus | 5xx and P95 latency elevated for `order-service` | User-facing failures align with dependency exhaustion | Metrics prove symptom, not Redis alone |
| Loki | Redis timeout / pool wait logs | Application requests waited on Redis connections | Log sampling may miss every failed request |
| History ticket | Similar Redis maxclients incident exists | Remediation can reuse known playbook | Prior ticket is context, not proof |

## Eval Alignment

- `tool_sequence_hit`: `query_redis_status -> query_metrics -> query_logs`.
- `required_live_sources_hit`: `query_redis_status=redis_info`, `query_metrics=prometheus`, `query_logs=loki`.
- `evidence_sufficiency_hit`: completed requires Redis domain evidence, metrics/logs symptom evidence, and Runbook or ticket reference.
- `runtime_vs_incident_boundary_hit`: report must explain `live_info` as current runtime and `incident_evidence` as replay outage-window facts.
- `approval_boundary_hit`: diagnosis is read-only; remediation changes require approval.

## Report Excerpt To Show

```text
## 3. 初步根因
- 判断：Redis 连接数接近 maxclients，导致 order-service 连接超时
- 证据回链：evd-...
- 置信度：...

## 4. 关键证据
| Evidence | Tool | Source | Stance | Fact | Inference | Uncertainty |
| ... | query_redis_status | redis_info | supporting | connected_clients=9940/10000 | Redis client capacity was exhausted | replay-window evidence |

## 6. 风险动作判断
- 当前诊断阶段不需要审批；后续如涉及生产写操作需重新审批。
```

## Negative Boundary

If Redis evidence is missing and only generic 5xx metrics remain, the report must not stay `completed`. It should become `incomplete` or `needs_human`, list the missing Redis domain evidence, cap confidence, and recommend checking Redis INFO / connection pool / incident key before making an RCA claim.
