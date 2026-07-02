# AIOps Full-Stack Sandbox

This sandbox gives AutoOnCall a reproducible live-data environment for AIOps demos. It runs Redis, MySQL, Prometheus, Alertmanager, Grafana, Loki, a read-only Kubernetes API mock, Tempo, Jaeger, OpenTelemetry Collector, Redpanda, and small mock services for CMDB, ticketing, and deployment history.

For a 10-minute campus-recruiting interview demo, use the main path in `README.md` first. This sandbox is the advanced path for proving that the same Tool Registry can consume adapter-backed evidence such as `redis_info`, `mysql`, `prometheus`, and `loki` instead of built-in mock output.

It is intentionally local-only demo infrastructure. Do not reuse the demo passwords in production.

All persistent Docker data is stored under:

```text
D:\AppDataStorage\DockerData\autooncall-full
```

## Services

| Service | Container | Host port | Purpose |
| --- | --- | ---: | --- |
| Redis | `autooncall-full-redis` | `16379` | Real `INFO`, `CONFIG GET maxclients`, Redis Stream timeline, and incident evidence keys |
| MySQL | `autooncall-full-mysql` | `13306` | Real `SHOW GLOBAL STATUS`, demo incident tables, SLO snapshots, and change correlation data |
| Metrics exporter | `autooncall-full-metrics-exporter` | `19108` | Deterministic service metrics in Prometheus text format |
| Prometheus | `autooncall-full-prometheus` | `19090` | Real HTTP API queried by `PrometheusMetricsAdapter` |
| Alertmanager | `autooncall-full-alertmanager` | `19093` | Local alert routing demo |
| Grafana | `autooncall-full-grafana` | `13000` | Dashboards and datasource exploration (`admin` / `admin`) |
| Loki | `autooncall-full-loki` | `13100` | Queryable incident logs for Redis/MySQL/K8s scenarios |
| Kubernetes mock | `autooncall-full-kubernetes-mock` | `18085` | Read-only Pod and Event API for `query_k8s_status` |
| Tempo | `autooncall-full-tempo` | `13200` | Trace backend for OpenTelemetry demos |
| Jaeger | `autooncall-full-jaeger` | `16686` | Trace UI for interview walkthroughs |
| Redpanda | `autooncall-full-redpanda` | `19092` | Kafka-compatible incident, deploy, and order event topics |
| CMDB mock | `autooncall-full-cmdb-mock` | `18081` | Service ownership and dependency fixtures |
| Ticketing mock | `autooncall-full-ticketing-mock` | `18083` | Historical incident fixtures |
| Deploy history mock | `autooncall-full-deploy-history-mock` | `18084` | Release/change correlation fixtures |

## Quick Start

Interview quick path:

1. Start the sandbox and seed demo data.
2. Load `deploy/sandbox.env` before starting FastAPI.
3. Run `redis_maxclients` or `mysql_slow_query` from the web UI.
4. Confirm the report/trace shows adapter-backed data sources such as `redis_info`, `mysql`, `prometheus`, or `loki`, not `mock`.

Full command path:

```powershell
make sandbox-up
powershell -ExecutionPolicy Bypass -File deploy\full-stack\seed-demo-data.ps1
make sandbox-demo
```

The demo writes a structured proof artifact to:

```text
logs/sandbox_aiops_simulation.json
```

The `data_sources` section should include live adapter sources such as `prometheus`, `redis_info`, and `mysql`. Failed scenarios, such as stopping Redis or MySQL, should appear as structured `failed` tool calls instead of crashing the Agent.

## Run FastAPI Against The Sandbox

Copy or load the settings in `deploy/sandbox.env` before starting FastAPI. The important switch is:

```text
AIOPS_MOCK_FALLBACK_ENABLED=false
```

This forces missing or broken adapters to return explicit structured failures instead of falling back to mock data.

For an interactive run:

```powershell
make sandbox-up
Get-Content deploy\sandbox.env | ForEach-Object {
  if ($_ -and -not $_.StartsWith("#") -and $_.Contains("=")) {
    $name, $value = $_.Split("=", 2)
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
  }
}
make dev
```

Then choose the Redis or MySQL AIOps scenarios in the web UI. The report and trace should mark adapter-backed evidence as `redis_info`, `mysql`, or `prometheus`, not `mock`.

## Ingest A Local Alertmanager Webhook

AutoOnCall also accepts Alertmanager-compatible webhook payloads through the normal API:

```powershell
curl.exe -X POST "http://127.0.0.1:9900/api/alerts/alertmanager" `
  -H "Content-Type: application/json" `
  -d "{\"receiver\":\"autooncall\",\"status\":\"firing\",\"alerts\":[{\"status\":\"firing\",\"labels\":{\"alertname\":\"RedisMaxClientsHigh\",\"service\":\"order-service\",\"environment\":\"prod\",\"severity\":\"critical\"},\"annotations\":{\"summary\":\"order-service Redis clients are near maxclients\"},\"startsAt\":\"2026-06-30T10:00:00Z\",\"fingerprint\":\"sandbox-redis-maxclients\"}]}"
```

The alert should appear in `GET /api/alerts` and create or update an `inc-alert-*` Incident visible through `GET /api/incidents`. Repeating the same fingerprint deduplicates the alert; sending `status=resolved` updates alert state without overwriting deeper Incident lifecycle states such as approval or change execution.

For an interview-friendly end-to-end demo, fetch a ready-made case and run it through the normal SSE workflow:

```powershell
curl.exe http://127.0.0.1:9900/api/aiops/demo/incidents/redis-maxclients
curl.exe -N -X POST http://127.0.0.1:9900/api/aiops/demo/incidents/redis-maxclients/run -H "Content-Type: application/json" -d "{}"
```

The Redis demo should collect evidence from `cmdb`, `redis_info`, `prometheus`, `loki`, `deploy_history`, `ticket_api`, and RAG runbooks when the knowledge base has been indexed. The K8s demo at `/api/aiops/demo/incidents/k8s-crashloop/run` should collect `kubernetes` evidence from the mock API rather than built-in mock tool data.

## Useful Checks

```powershell
docker compose -f deploy/compose/full-stack-compose.yml ps
curl http://127.0.0.1:19108/metrics
curl "http://127.0.0.1:19090/api/v1/query?query=autooncall_p95_latency_ms%7Bservice%3D%22order-service%22%7D"
curl "http://127.0.0.1:13100/loki/api/v1/labels"
curl "http://127.0.0.1:18085/api/v1/namespaces/default/pods?labelSelector=app=inventory-service"
docker exec autooncall-full-redis redis-cli INFO clients
docker exec autooncall-full-mysql mysql -uautooncall -pautooncall123 -D autooncall -e "SELECT case_id, expected_root_cause FROM incident_demo_cases;"
docker exec autooncall-full-redpanda rpk topic list
```

## Reset

```powershell
make sandbox-down
make sandbox-up
```

The full-stack sandbox uses persistent bind mounts. To reset or refresh the interview demo evidence, rerun:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\full-stack\seed-demo-data.ps1
```
