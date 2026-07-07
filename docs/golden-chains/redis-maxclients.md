# Redis Maxclients Portfolio Report

This is the main Redis portfolio case for interviews. It demonstrates a live adapter-backed AIOps chain: Redis evidence comes from the local Docker Redis adapter, symptoms come from Prometheus and Loki, and remediation is bounded by approval policy.

## Portfolio Card

| Item | Value |
| --- | --- |
| Case ID | `redis_maxclients_timeout` |
| Service | `order-service` |
| Severity | `P1` / `critical` |
| Main signal | `connected_clients=9940/10000`, `blocked_clients=37` |
| User impact | 5xx spike and P95 latency elevation |
| Live sources | `redis_info`, `prometheus`, `loki`, `ticket_api`, multi-source RAG |
| Eval status | PASS, `completed`, confidence `0.72`, risk policy `allow` |

## Alert Payload

```json
{
  "receiver": "autooncall",
  "status": "firing",
  "commonLabels": {"environment": "prod", "cluster": "prod-a"},
  "alerts": [{
    "labels": {
      "alertname": "RedisMaxClientsNearLimit",
      "service": "order-service",
      "severity": "critical",
      "redis_instance": "redis-cluster-prod"
    },
    "annotations": {
      "summary": "order-service Redis connected_clients is above 99% of maxclients",
      "description": "Redis connection timeout and 5xx spike; connected_clients=9940 maxclients=10000 blocked_clients=37",
      "runbook": "aiops-docs/redis_maxclients.md"
    },
    "startsAt": "2026-07-06T10:00:00Z"
  }]
}
```

## Tool Chain

| Stage | Expected tool | Actual tool | Source | What it proves |
| --- | --- | --- | --- | --- |
| 1 | `query_redis_status` | `query_redis_status` | `redis_info` | Redis incident-window evidence shows near-maxclient saturation |
| 2 | `query_metrics` | `query_metrics` | `prometheus` | 5xx and P95 latency increased during the incident window |
| 3 | `query_logs` | `query_logs` | `loki` | Application logs contain Redis timeout / pool wait symptoms |
| 4 | `search_runbook` | `search_runbook` | `redis_postmortem.pdf`, `tickets.csv` | PDF postmortem and historical ticket table join the RCA evidence chain |
| 5 | `search_history_ticket` | `search_history_ticket` | `ticket_api` | Similar Redis maxclients incident exists |
| 6 | `suggest_remediation` | `suggest_remediation` | `rule_based` | Produces non-executing remediation guidance |

## Evidence Table

| Evidence | Fact | Inference | Uncertainty |
| --- | --- | --- | --- |
| Redis incident key | `connected_clients=9940/maxclients=10000`, `blocked_clients=37` | Redis accepted connections reached the configured ceiling and clients began waiting/timeouting | This is incident-window replay evidence, not necessarily the current runtime state |
| Prometheus | `order-service` 5xx and P95 increased | User-facing errors align with Redis dependency saturation | Metrics prove impact and timing, not Redis root cause alone |
| Loki | Redis timeout and pool-wait logs | Application requests waited on Redis connections | Log sampling may miss some failed requests |
| PDF postmortem | `redis_postmortem.pdf` records `connected_clients=9940`, `maxclients=10000`, `blocked_clients=37` | Knowledge evidence confirms the outage-window interpretation and approval boundary | Postmortem is retrospective evidence and must be paired with live signals |
| Historical ticket | Similar Redis maxclients incident | Prior ticket supports the remediation playbook | Historical similarity is advisory, not proof |
| CSV ticket table | `tickets.csv` row `ticket_id=INC-REDIS-001` links root cause and approved resolution | Historical experience supports the chosen remediation path | Table rows are historical context, not live proof |
| Runbook | Maxclients investigation checklist | Gives a safe operator workflow | Eval fixture is deterministic offline content |

## Runtime Vs Incident Window

- `live_info` is the current Docker Redis runtime. It proves the adapter is connected to the real container.
- `incident_evidence` is the outage-window record stored in Redis key `autooncall:incident:order-service:redis-maxclients`.
- The report must not claim the current container is still saturated if current `live_info.connected_clients` is low.
- The RCA is based on the incident-window fact `connected_clients=9940/10000`; current runtime is used only as adapter proof.

## Root Cause

Redis client capacity was exhausted during the incident window. `order-service` hit Redis connection timeouts while service 5xx and P95 latency increased. The strongest hypothesis is Redis maxclients / application connection-pool exhaustion rather than CPU, memory, disk, or K8s failure.

## Remediation Approval Boundary

Read-only diagnosis can complete without approval. The following actions require human approval and a change window:

- Increase Redis `maxclients` or resize Redis capacity.
- Restart Redis or related application pods.
- Change application Redis pool size, timeout, or retry policy.
- Apply traffic throttling that affects production users.

Safe immediate guidance is to reduce retry storms, inspect hot keys and idle clients, confirm client pool usage, and prepare an approved capacity/configuration change.

## Eval Summary

Latest verified command:

```powershell
.\.venv\Scripts\python.exe scripts\eval\eval_cases.py --cases eval\cases.yaml --env-file deploy\sandbox.env --report-path logs\eval_reports.db --summary-json logs\eval_summary.json --summary-md logs\eval_summary.md
```

Portfolio metrics to show:

| Metric | Result |
| --- | --- |
| Overall eval | `42/42` passed |
| AIOps eval | `16/16` passed |
| RAG eval | `26/26` passed |
| `required_live_sources_hit` | PASS |
| `evidence_sufficiency_hit` | PASS |
| `runtime_vs_incident_boundary_hit` | PASS |
| `approval_boundary_hit` | PASS |

## Interview Talk Track

Start with the alert payload, then show the tool chain in order. The key sentence is: "The current Redis container proves live adapter connectivity; the seeded Redis incident key preserves the outage-window facts. I keep those two evidence scopes separate so the Agent does not overclaim."
