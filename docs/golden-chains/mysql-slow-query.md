# MySQL Slow Query Portfolio Report

This is the main MySQL portfolio case for interviews. It demonstrates a live adapter-backed database diagnosis chain: MySQL evidence comes from the local Docker MySQL adapter, symptoms come from Prometheus and Loki, and remediation is explicitly separated from production execution.

## Portfolio Card

| Item | Value |
| --- | --- |
| Case ID | `mysql_slow_query_latency` |
| Service | `payment-service` |
| Severity | `P1` / `critical` |
| Main signal | `slow_queries=18`, `active_connections=188/200`, `pool_waiting=6` |
| User impact | Payment latency and checkout degradation |
| Live sources | `mysql`, `prometheus`, `loki`, `deploy_history`, `ticket_api` |
| Eval status | PASS, `completed`, confidence `0.72`, risk policy `allow` |

## Alert Payload

```json
{
  "receiver": "autooncall",
  "status": "firing",
  "commonLabels": {"environment": "prod", "cluster": "prod-a"},
  "alerts": [{
    "labels": {
      "alertname": "MySQLSlowQueryLatency",
      "service": "payment-service",
      "severity": "critical",
      "mysql_instance": "payment-mysql"
    },
    "annotations": {
      "summary": "payment-service p95 latency high with MySQL slow query and pool waiting",
      "description": "slow query avg_ms=920 count=18; active connections=188/200; pool_waiting=6",
      "runbook": "aiops-docs/mysql_slow_query.md"
    },
    "startsAt": "2026-07-06T10:00:00Z"
  }]
}
```

## Tool Chain

| Stage | Expected tool | Actual tool | Source | What it proves |
| --- | --- | --- | --- | --- |
| 1 | `query_mysql_status` | `query_mysql_status` | `mysql` | Slow queries, active connections, and pool waiting are present in incident evidence |
| 2 | `query_metrics` | `query_metrics` | `prometheus` | Payment latency and error symptoms increased |
| 3 | `query_logs` | `query_logs` | `loki` | Application logs show payment timeout / slow SQL symptoms |
| 4 | `search_runbook` | `search_runbook` | `eval_fixture` | Runbook gives safe SQL investigation steps |
| 5 | `search_history_ticket` | `search_history_ticket` | `ticket_api` | Similar slow query / pool waiting incident exists |
| 6 | `suggest_remediation` | `suggest_remediation` | `rule_based` | Produces non-executing remediation guidance |

## Evidence Table

| Evidence | Fact | Inference | Uncertainty |
| --- | --- | --- | --- |
| MySQL incident table | `slow_queries=18`, `active_connections=188/200`, `pool_waiting=6` | Slow SQL occupied database connections and backed up the application pool | Current `SHOW GLOBAL STATUS` counters can differ from the replay window |
| Prometheus | Payment P95/error signals elevated | Users saw latency and failed payment attempts | Metrics identify impact, not the SQL digest alone |
| Loki / payment event | Payment timeout and checkout degradation logs | Application impact aligns with DB wait | Logs can be sampled and may not include every timeout |
| Deploy history | Recent release context exists | Release timing helps judge whether a query path changed | Deployment correlation is supporting context |
| Historical ticket | Similar slow query / pool wait incident | Prior remediation informs the next safe action | Historical match is advisory, not proof |

## Runtime Vs Incident Window

- `live_status` is the current Docker MySQL runtime. It proves the adapter can query the real container.
- `incident_evidence` / payment event rows preserve the outage-window facts: `slow_queries=18`, `active_connections=188/200`, `pool_waiting=6`.
- The report must not claim the current MySQL process still has 18 active slow queries if runtime counters are normal.
- The RCA is based on the incident-window business evidence, with current runtime used as adapter proof and health context.

## Root Cause

The strongest hypothesis is a slow SQL path in `payment-service` holding MySQL connections long enough to create connection-pool waiting. This explains the observed payment latency and checkout degradation better than generic CPU, memory, disk, or K8s explanations.

## Remediation Approval Boundary

Read-only diagnosis can complete without approval. The following actions require human approval and a change window:

- Add or change MySQL indexes.
- Rewrite SQL or change ORM query behavior.
- Change database or application connection-pool parameters.
- Restart MySQL or payment service instances.
- Run data-changing SQL or operational scripts.

Safe immediate guidance is to capture the SQL digest and `EXPLAIN`, reduce the expensive payment path behind a flag if available, verify lock waits and pool settings, and prepare an approved SQL/index/pool change.

## Eval Summary

Latest verified command:

```powershell
.\.venv\Scripts\python.exe scripts\eval\eval_cases.py --cases eval\cases.yaml --env-file deploy\sandbox.env --report-path logs\eval_reports.db --summary-json logs\eval_summary.json --summary-md logs\eval_summary.md
```

Portfolio metrics to show:

| Metric | Result |
| --- | --- |
| Overall eval | `41/41` passed |
| AIOps eval | `16/16` passed |
| RAG eval | `25/25` passed |
| `required_live_sources_hit` | PASS |
| `evidence_sufficiency_hit` | PASS |
| `runtime_vs_incident_boundary_hit` | PASS |
| `approval_boundary_hit` | PASS |

## Interview Talk Track

Start with the alert payload, then show how MySQL evidence comes before generic metrics/logs. The key sentence is: "The Agent can explain why MySQL is the likely cause, but it cannot execute SQL, add indexes, or change pool parameters without a human-approved change path."
