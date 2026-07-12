# External Knowledge Sources

This file records the official documentation snapshots included directly in
the AutoOnCall default knowledge corpus. The snapshots expand the original
interview corpus with realistic competing material, so RAG evaluation now
measures retrieval against both project-specific runbooks and upstream
operational documentation.

Downloaded: 2026-07-10

| Local file | Upstream source | License | Intended use |
| --- | --- | --- | --- |
| `official_kubernetes_debug_pods.md` | `https://github.com/kubernetes/website/blob/main/content/en/docs/tasks/debug/debug-application/debug-pods.md` | CC BY 4.0 | Pod state, events, logs, container debugging |
| `official_kubernetes_debug_services.md` | `https://github.com/kubernetes/website/blob/main/content/en/docs/tasks/debug/debug-application/debug-service.md` | CC BY 4.0 | Service, EndpointSlice, DNS and network-path diagnosis |
| `official_kubernetes_pod_failure_reason.md` | `https://github.com/kubernetes/website/blob/main/content/en/docs/tasks/debug/debug-application/determine-reason-pod-failure.md` | CC BY 4.0 | Container exit status and pod failure analysis |
| `official_prometheus_alerting_practices.md` | `https://github.com/prometheus/docs/blob/main/docs/practices/alerting.md` | Apache-2.0 | Alert design, symptom-based alerting and runbook links |
| `official_prometheus_alerting_rules.md` | `https://github.com/prometheus/prometheus/blob/main/docs/configuration/alerting_rules.md` | Apache-2.0 | Alerting rule syntax, pending/firing state and templates |
| `official_redis_clients.md` | `https://github.com/redis/docs/blob/main/content/develop/reference/clients.md` | CC BY-NC-SA 4.0 and upstream notices | Client limits, maxclients and connection behavior |
| `official_redis_latency.md` | `https://github.com/redis/docs/blob/main/content/operate/oss_and_stack/management/optimization/latency.md` | CC BY-NC-SA 4.0 and upstream notices | Latency sources, measurement and troubleshooting |
| `official_loki_troubleshoot_ingest.md` | `https://grafana.com/docs/loki/latest/operations/troubleshooting/troubleshoot-ingest.md` | Grafana documentation terms; upstream repository AGPL-3.0 | Loki write-path and ingestion troubleshooting |
| `official_loki_troubleshoot_query.md` | `https://grafana.com/docs/loki/latest/operations/troubleshooting/troubleshoot-query.md` | Grafana documentation terms; upstream repository AGPL-3.0 | Loki read-path and query troubleshooting |

## Corpus Boundary

- These snapshots live directly under `docs/knowledge-base/` and are included
  by the existing non-recursive upload and offline evaluation commands.
- The original project-specific runbooks remain in the same corpus. Evaluation
  results must therefore record the complete knowledge-asset manifest and must
  not be compared with the old 11-file baseline without noting the corpus
  change.
- Add dedicated official-document cases covering relevance, ambiguity, refusal
  behavior, citation quality, and retrieval latency before making quality
  claims about the expanded topics.
- Preserve the source URL and license attribution when redistributing a
  snapshot. Redis documentation has a non-commercial license condition, so it
  must not be repackaged for commercial distribution without a separate review.

## Integrity

The file hashes below identify the exact downloaded snapshots.

| Local file | SHA-256 |
| --- | --- |
| `official_kubernetes_debug_pods.md` | `FA0695BDCEE4608CF1EBD4D7A3891E353E7764E566EB40EF738C5B98F5BC3645` |
| `official_kubernetes_debug_services.md` | `75D0D26D15E57B42033B09569D181ECF33AC7B6CF719E27C82093CBE6EE419F6` |
| `official_kubernetes_pod_failure_reason.md` | `B43A3DAD47D2428042E5A5D2B32C586FA1B0504CDCDFD9513874B8BFCE35F57D` |
| `official_loki_troubleshoot_ingest.md` | `F82648CCDE3DA0B412A28F1BA1D9F7DCA5AF81BD97723BD48C27B2964C45C764` |
| `official_loki_troubleshoot_query.md` | `B9227A2AF07F26E251F7F53AC5ACCD2D4710811F33F1973CF90456C3AE088F7B` |
| `official_prometheus_alerting_practices.md` | `2DF77104B4764DE91FB3792EE81DB29E54DD50DB301B669ECA4D0591DA409786` |
| `official_prometheus_alerting_rules.md` | `92085DEDF36AE2A9F16AA008931379815397862A9AEE835F888CE087A741B7B5` |
| `official_redis_clients.md` | `38E1A87B669D1B7DEE75F1D4C44DFF95160F08397DADA30704AD747D59F72232` |
| `official_redis_latency.md` | `CAD8B108A27D421C9192EE36074502CDD27D77A584AE9CFBAD3D4116536AB9E9` |
