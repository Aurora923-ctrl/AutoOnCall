# AIOps Local Docker Stacks

This sandbox gives AutoOnCall a reproducible live-data environment for AIOps demos. The default interview stack runs Redis, MySQL, metrics-exporter, Prometheus, Loki, and loki-log-emitter. Service catalog, deployment history, and historical tickets are seeded into the real MySQL container instead of HTTP mock services. Milvus/RAG remains a bonus path via `make up && make upload`; it is not part of the default live AIOps stack. The interview stack intentionally omits advanced adapters such as Grafana, Alertmanager, tracing backends, OpenTelemetry Collector, Redpanda, and local Kubernetes mocks.

For a 5-minute campus-recruiting interview demo, use `make interview-up`. Advanced observability/message-queue services are intentionally not part of this stack.

It is intentionally local-only demo infrastructure. Do not reuse the demo passwords in production.

All persistent Docker data is stored under:

```text
D:\AppDataStorage\DockerData\autooncall-full
```

## Services

| Service | Container | Host port | Purpose |
| --- | --- | ---: | --- |
| Redis | `autooncall-full-redis` | `16379` | Real `INFO`, `CONFIG GET maxclients`, Redis Stream timeline, and incident evidence keys |
| MySQL | `autooncall-full-mysql` | `13306` | Real `SHOW GLOBAL STATUS`, live incident evidence tables, SLO snapshots, service catalog, deploy history, and ticket data |
| Metrics exporter | `autooncall-full-metrics-exporter` | `19108` | Deterministic service metrics in Prometheus text format |
| Prometheus | `autooncall-full-prometheus` | `19090` | Real HTTP API queried by `PrometheusMetricsAdapter` |
| Loki | `autooncall-full-loki` | `13100` | Queryable incident logs for Redis/MySQL scenarios |
| Loki log emitter | `autooncall-full-loki-log-emitter` | - | Periodically pushes deterministic incident logs into Loki |

Seed data is not kept as a long-running or one-shot Compose service in the default interview stack. `make interview-up` starts only the six core services above, then runs `scripts/sandbox/seed_live_incident_evidence.py --acknowledge-local-only` to idempotently write service catalog, deployment history, historical tickets, and incident-window evidence into allowlisted local MySQL and Redis containers.

## Quick Start

Interview quick path:

1. Start the interview stack.
2. Load `deploy/sandbox.env` before starting FastAPI.
3. Run `redis_maxclients` or `mysql_slow_query` from the web UI.
4. Confirm the report/trace shows adapter-backed data sources such as `redis_info`, `mysql`, `prometheus`, or `loki`, not `mock`.

Full command path:

```powershell
make interview-up
make sandbox-verify
.venv\Scripts\python.exe scripts\eval\eval_cases.py `
  --cases eval\cases.yaml `
  --env-file deploy\sandbox.env `
  --report-path logs\live_golden_eval_reports.db `
  --summary-json logs\live_golden_eval_summary_current.json `
  --summary-md logs\live_golden_eval_summary_current.md `
  --skip-rag `
  --live-golden
.venv\Scripts\python.exe scripts\eval\eval_rag_cases.py `
  --cases eval\rag_cases.yaml `
  --docs-dir docs/knowledge-base `
  --summary-json logs\rag_eval_summary_current.json `
  --summary-md logs\rag_eval_summary_current.md
.\.venv\Scripts\python.exe scripts\eval\verify_milvus_multisource_rag.py `
  --summary-json logs\milvus_multisource_verification.json `
  --summary-md logs\milvus_multisource_verification.md
.venv\Scripts\python.exe scripts\eval\build_interview_summary.py `
  --summary-json logs\interview_eval_summary.json `
  --summary-md logs\interview_eval_summary.md
```

The verification writes a structured proof artifact to:

```text
logs/full_stack_adapter_verification.json
```

The Milvus multi-source proof is written to:

```text
logs/milvus_multisource_verification.md
```

It verifies that Redis/MySQL PDF postmortems, HTML Wiki pages, CSV tickets, and XLSX deploy history are inserted into a real Milvus collection and can be retrieved by source-targeted probes.

The `data_sources` section should include live adapter sources such as `prometheus`, `loki`, `redis_info`, `mysql`, `cmdb`, `deploy_history`, and `ticket_api`. In the interview stack, `cmdb`, `deploy_history`, and `ticket_api` are backed by MySQL seed tables rather than HTTP mock containers. Failed scenarios, such as stopping Redis or MySQL, should appear as structured `failed` tool calls instead of crashing the Agent.

## Golden Redis/MySQL Live Chains

The interview stack is also the reference environment for the two highest-value live golden chains:

| Chain | Alertmanager label | Required live sources | Expected first tools |
| --- | --- | --- | --- |
| Redis maxclients exhausted | `redis_instance=redis-cluster-prod` | `query_redis_status=redis_info`, `query_metrics=prometheus`, `query_logs=loki`, `search_history_ticket=ticket_api` | `query_redis_status -> query_metrics -> query_logs` |
| MySQL slow query latency | `mysql_instance=payment-mysql` | `query_mysql_status=mysql`, `query_metrics=prometheus`, `query_logs=loki`, `search_history_ticket=ticket_api` | `query_mysql_status -> query_metrics -> query_logs` |

These chains are intentionally not allowed to fall back to mock data when the sandbox environment is loaded. `search_runbook` can still use the deterministic eval fixture in offline evaluation, because the live container proof is about Redis, MySQL, Prometheus, Loki, and ticket data sources.

K8s CrashLoop/OOMKilled is intentionally treated as an offline golden regression case for the default interview. Do not claim it is live container-backed unless a real Kubernetes API or a deliberately scoped lightweight fixture is added.

Run the live golden evaluation against the Docker containers:

```powershell
.venv\Scripts\python.exe scripts\eval\eval_cases.py `
  --cases eval\cases.yaml `
  --env-file deploy\sandbox.env `
  --report-path logs\live_golden_eval_reports.db `
  --summary-json logs\live_golden_eval_summary_current.json `
  --summary-md logs\live_golden_eval_summary_current.md `
  --skip-rag `
  --live-golden
```

The expected result is `16/16 cases passed`. The interview-facing rollup is generated by `scripts/eval/build_interview_summary.py` and written to `logs/interview_eval_summary.md`. For Redis and MySQL, `logs/live_golden_eval_summary_current.json` should show:

- `passed=true` and `failed_metrics=[]`.
- `required_live_sources_hit=true`.
- `tool_sources` matching the required live sources above.
- `golden_chain.trace_completeness_basis` with `trace_id`, tool-call count, evidence count, and all tool-call statuses.
- `approval.diagnosis_needs_approval=false` and `approval.remediation_change_requires_approval=true`.
- `logs/full_stack_adapter_verification.json` with `status=passed`, `missing_sources=[]`, `failed_tools=[]`, and `mock_fallback_detected=false`.

MySQL remediation must state the operational boundary clearly: diagnosis is read-only; executing SQL rewrites, adding indexes, changing connection-pool or database parameters, or restarting the database requires human approval and a change window.

For the Redis chain, keep this wording explicit during the interview:

- `live_info` is the current Redis container runtime state. It proves the adapter is connected to the real container and shows what Redis looks like now.
- `incident_evidence` is the replay incident-window evidence stored in the real Redis key `autooncall:incident:order-service:redis-maxclients`. It is the evidence used to explain the simulated outage window, for example `connected_clients=9940/maxclients=10000`.
- Do not claim the current Redis container is still saturated if `live_info.connected_clients` is low; say the current runtime is healthy/idle while the seeded incident key preserves the outage-window facts.

The Prometheus demo alerts are loaded from `deploy/adapters/alert-rules.yml` through `rule_files` in `deploy/adapters/prometheus.yml`. The rule expressions intentionally match the exporter metrics: `autooncall_http_5xx_rate` and `autooncall_p95_latency_ms`.

To avoid Windows PowerShell code-page mojibake during interviews, present generated UTF-8 artifacts or the web UI instead of raw terminal Chinese output. Prefer:

- `logs/interview_eval_summary.md` as the single interview entry point.
- `logs/live_golden_eval_summary_current.md` and `logs/live_golden_eval_summary_current.json` for live AIOps details.
- `logs/rag_eval_summary_current.md` for standalone RAG retrieval details.
- `logs/sandbox_aiops_simulation.json`.
- The AIOps web UI report/trace view.

If you must inspect files in PowerShell, use `Get-Content <path> -Encoding utf8` or switch the console to UTF-8 first.

## Run FastAPI Against The Interview Stack

Copy or load the settings in `deploy/sandbox.env` before starting FastAPI. The important switch is:

```text
AIOPS_MOCK_FALLBACK_ENABLED=false
```

This forces missing or broken adapters to return explicit structured failures instead of falling back to mock data.

For an interactive run:

```powershell
make interview-up
Get-Content deploy\sandbox.env | ForEach-Object {
  if ($_ -and -not $_.StartsWith("#") -and $_.Contains("=")) {
    $name, $value = $_.Split("=", 2)
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
  }
}
make dev
```

Then choose the Redis or MySQL AIOps scenarios in the web UI. The report and trace should mark adapter-backed evidence as `redis_info`, `mysql`, or `prometheus`, not `mock`.

## Useful Checks

```powershell
docker compose -f deploy/compose/interview-stack.yml ps
curl http://127.0.0.1:19108/metrics
curl "http://127.0.0.1:19090/api/v1/query?query=autooncall_p95_latency_ms%7Bservice%3D%22order-service%22%7D"
curl "http://127.0.0.1:13100/loki/api/v1/labels"
docker exec autooncall-full-redis redis-cli INFO clients
docker exec autooncall-full-mysql mysql -uautooncall -pautooncall123 -D autooncall -e "SELECT incident_key, source, expected_root_cause FROM aiops_incident_evidence;"
docker exec autooncall-full-mysql mysql -uautooncall -pautooncall123 -D autooncall -e "SELECT service_name, business_domain FROM aiops_service_catalog;"
docker exec autooncall-full-mysql mysql -uautooncall -pautooncall123 -D autooncall -e "SELECT ticket_id, service_name, severity FROM aiops_history_tickets;"
```

## Reset

```powershell
make interview-down
make interview-up
```

The interview stack uses persistent bind mounts. To reset the containers, run:

```powershell
make interview-down
make interview-up
```
