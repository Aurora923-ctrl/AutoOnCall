# Production Configuration Notes

AutoOnCall can run in local demo mode with mock/fallback data, but production-oriented deployments should make data sources, authentication, storage, and retention explicit. The safest source of configuration defaults is `app/config.py`; a copyable environment template is available in `.env.example`.

## Recommended Defaults

- Treat all FastAPI routes as internal tooling; do not expose the service directly to the public internet.
- Set `DEBUG=false`.
- Set `CORS_ALLOWED_ORIGINS` to explicit internal frontend origins.
- Treat startup warnings about non-local bind, disabled API auth, or wildcard CORS as release blockers.
- Store `DASHSCOPE_API_KEY`, API tokens, Redis passwords, MySQL credentials, bearer tokens, and webhook secrets in a secret manager.
- Require SSO/OIDC or an internal admin token for diagnosis, approval, upload, indexing, report, change, and alert-ingestion APIs.
- Add RBAC for read-only incident viewing, diagnosis execution, alert ingestion, approval decisions, safe-change operations, and admin actions before exposing the service beyond a trusted network.
- Enable `API_AUTH_ENABLED=true` for stricter internal environments and provide role tokens; for production, prefer SSO/OIDC and map identities to the same read, diagnose, approve, change, and admin scopes.
- Use persistent storage for `AIOPS_SQLITE_PATH` in single-replica deployments, or set `AIOPS_STORAGE_BACKEND=mysql` with `MYSQL_DSN` before multi-replica deployments.
- Back up the configured AIOps state store. SQLite needs file-level backup; MySQL should use managed database backup.
- Set `AIOPS_MOCK_FALLBACK_ENABLED=false` in staging and production checks when unconfigured adapters must return structured `failed/not_configured` evidence instead of deterministic mock data.
- Keep `AIOPS_STORE_RAW_EXTERNAL_PAYLOAD=false` unless temporarily debugging. With the default value, external responses and Alertmanager webhook payloads are stored in compact form.
- Keep `MILVUS_RECREATE_ON_DIMENSION_MISMATCH=false`; rebuild mismatched collections only through an explicit maintenance procedure.
- Keep `INDEX_ALLOWED_ROOTS` limited to trusted knowledge-base directories such as `uploads,aiops-docs`.
- Use `/health/live` for process liveness and `/health/ready` for dependency and adapter readiness.

## Internal API Auth

- `API_AUTH_ENABLED=false` keeps local demos open.
- `API_READ_TOKEN`: read-only token for chat, incident, alert, trace, report, approval list, eval, and tool contract views.
- `API_OPERATOR_TOKEN`: read + diagnosis + knowledge indexing + alert ingestion token.
- `API_APPROVER_TOKEN`: read + approval decision + safe-change resume/manual-result token.
- `API_ADMIN_TOKEN`: all scopes.
- `API_AUTH_TOKENS`: optional JSON map for multiple tokens, for example `{"ops-token":["operator"],"sre-token":["approver"]}`.

Clients can send either `Authorization: Bearer <token>` or `X-AutoOnCall-Token: <token>`. If auth is enabled but no token is configured, protected APIs return 503 so the service fails closed.

## External Adapters

- `ALERTMANAGER_BASE_URL`: enables read-only alert queries through `query_alerts`.
- `PROMETHEUS_BASE_URL`: enables real metrics in `query_metrics`.
- `LOG_GATEWAY_URL` or `LOKI_BASE_URL`: enables real logs in `query_logs`.
- `JAEGER_BASE_URL` or `TEMPO_BASE_URL`: enables trace lookups in `query_traces`.
- `REDPANDA_ADMIN_URL`: enables Redpanda/Kafka lag evidence.
- `KUBERNETES_API_SERVER`: enables read-only Pod and Event status in `query_k8s_status`.
- `REDIS_URL` or `REDIS_HOST`/`REDIS_PORT`: enables Redis readiness and `INFO` evidence.
- `MYSQL_DSN`, `MYSQL_URL`, or split MySQL host fields: enables MySQL readiness and read-only status evidence.
- `CMDB_API_URL`: enables service ownership and topology context.
- `DEPLOY_HISTORY_API_URL`: enables recent release/change correlation evidence.
- `TICKET_API_URL`: enables historical incident search.

All adapters are fail-soft: missing config keeps the existing MCP/mock path when `AIOPS_MOCK_FALLBACK_ENABLED=true`; when it is false, missing config returns structured `failed/not_configured` tool output without crashing the Agent. External failures always return structured `failed` tool output.

Use least-privilege external accounts. MySQL diagnosis users should only need `SELECT` and minimal `SHOW` permissions. Redis production users may not have `CONFIG` or `SLOWLOG`; disable those commands with `REDIS_ALLOW_ADMIN_COMMANDS=false` and rely on `INFO` signals.

## Alertmanager Webhook

External Alertmanager webhooks enter through:

```text
POST /api/alerts/alertmanager
```

The endpoint normalizes `alerts[]`, deduplicates by fingerprint, creates or updates `IncidentState`, and writes `AlertEvent` records. In production-facing setups, protect this endpoint with the same internal auth or gateway-level allowlist used for diagnosis APIs. Keep full raw payload storage disabled unless debugging sensitive webhook issues.

## Runtime Retention

SQLite stores runtime state by default; MySQL can be enabled with `AIOPS_STORAGE_BACKEND=mysql`. The cleanup script supports both backends:

```powershell
.\venv\Scripts\python.exe scripts\cleanup_aiops_store.py --database data\aiops_state.db --keep-days 14 --dry-run
.\venv\Scripts\python.exe scripts\cleanup_aiops_store.py --database data\aiops_state.db --keep-days 14
```

For the configured backend, omit `--database`:

```powershell
.\venv\Scripts\python.exe scripts\cleanup_aiops_store.py --keep-days 14 --dry-run
```

The cleanup command deletes old records from `alert_events`, `trace_events`, `approval_requests`, `diagnosis_reports`, `change_executions`, `aiops_sessions`, and `incident_states`.

SQLite-to-MySQL migration:

```powershell
.\venv\Scripts\python.exe scripts\migrate_aiops_sqlite_to_mysql.py --sqlite data\aiops_state.db --dry-run
.\venv\Scripts\python.exe scripts\migrate_aiops_sqlite_to_mysql.py --sqlite data\aiops_state.db
```

## Minimal Health Gate

```powershell
.\venv\Scripts\python.exe -m compileall app scripts tests
.\venv\Scripts\python.exe -m ruff check app scripts tests
.\venv\Scripts\python.exe -m pytest tests -q
.\venv\Scripts\python.exe scripts\eval_cases.py --cases eval\cases.yaml --report-path logs\eval_reports.db --summary-json logs\eval_summary.json --summary-md logs\eval_summary.md
```
