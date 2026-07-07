# MySQL Slow Query Golden Chain

## 5-Minute Walkthrough

MySQL is a live adapter-backed golden chain. The key business story is slow SQL occupying database connections, which causes pool waiting and payment latency.

1. Start from the Alertmanager payload: `MySQLSlowQueryHigh` for `payment-service`, `mysql_instance=payment-mysql`, severity `critical`.
2. Incident fields normalize to `service_name=payment-service`, `severity=P1`, `environment=prod`, symptom `MySQL slow query and connection pool waiting`.
3. Planner checks MySQL first, then Prometheus and Loki symptoms, then Runbook/history ticket context.
4. Evidence shows `slow_queries=18`, `active_connections=188/200`, `pool_waiting=6`, elevated latency, and payment errors.
5. Root cause is slow SQL holding connections long enough to create connection-pool waiting.
6. Diagnosis is read-only; SQL rewrite, index changes, pool/db parameter changes, or database restart require approval.

## Fixed Chain Contract

- Alertmanager payload: `eval/cases.yaml#mysql_slow_query_latency`.
- Incident fields: `payment-service`, `P1`, `prod`, MySQL slow query and latency symptom.
- Planner expected steps: `query_mysql_status -> query_metrics -> query_logs -> search_runbook -> search_history_ticket`.
- Actual tool order requirement: MySQL status before metrics/logs.
- Evidence fields: every evidence item must expose `fact`, `inference`, and `uncertainty`.
- Root cause: MySQL slow query caused connection pool waiting and request latency.
- Remediation: identify digest/EXPLAIN, reduce high-cost path, use temporary traffic controls, apply SQL/index/pool changes only through approved change.
- Approval: diagnosis no; remediation change yes.
- Report must contain: MySQL evidence chain, slow SQL, active connections, pool waiting, user impact, approval boundary.
- Eval case: `mysql_slow_query_latency`.

## Evidence Checklist

| Evidence | Fact | Inference | Uncertainty |
| --- | --- | --- | --- |
| MySQL incident table | `slow_queries=18`, `active_connections=188/200`, `pool_waiting=6` | Slow SQL is consuming connection capacity and backing up the pool | Runtime `Slow_queries` counter can be 0; incident evidence carries the outage window |
| Prometheus | Payment P95/error signals elevated | Users are seeing latency and failed payments | Metrics do not identify SQL digest alone |
| Loki/payment event | Payment timeout or checkout degradation logs | Application impact aligns with DB wait | Logs can be sampled |
| History ticket | Similar slow query / pool wait incident | Prior remediation informs next action | Historical match is advisory |

## Eval Alignment

- `tool_sequence_hit`: `query_mysql_status -> query_metrics -> query_logs`.
- `required_live_sources_hit`: `query_mysql_status=mysql`, `query_metrics=prometheus`, `query_logs=loki`, `search_history_ticket=ticket_api`.
- `evidence_sufficiency_hit`: completed requires MySQL domain evidence, metrics/logs symptom evidence, and Runbook or ticket reference.
- `runtime_vs_incident_boundary_hit`: report must explain `live_status` as current runtime and `incident_evidence` as outage-window facts.
- `approval_boundary_hit`: read-only diagnosis can finish; SQL/index/pool changes require approval.

## Report Excerpt To Show

```text
## 3. 初步根因
- 判断：MySQL 慢查询、连接池等待或锁等待放大接口延迟
- 证据回链：evd-...
- 置信度：...

## 4. 关键证据
| Evidence | Tool | Source | Stance | Fact | Inference | Uncertainty |
| ... | query_mysql_status | mysql | supporting | slow_query_count=18, active_connections=188/200, pool_waiting=6 | Slow SQL occupied connections and caused pool waiting | current runtime counter may be normal |

## 8. 回滚 / 观察指标
- 观察 payment P95、5xx、active connections、pool waiting、慢 SQL 数量。
```

## Negative Boundary

If MySQL status or business incident evidence is unavailable, metrics/logs alone can prove payment latency but not the database RCA. The report should downgrade, list missing MySQL domain evidence, and ask an operator to check slow SQL digest, EXPLAIN, locks, and connection-pool state.
