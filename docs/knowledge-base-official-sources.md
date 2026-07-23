# External Knowledge Sources

This file records the retrieval-focused official documentation snapshots included in the
AutoOnCall knowledge corpus.

## Snapshot provenance

- The previous local snapshots were originally added in repository commit
  `0e428720ae94db6086d9c37323ae13d644d054f2`, but their recorded hashes no longer
  matched the working files.
- On `2026-07-21`, each source was downloaded again from the pinned upstream commit below
  and deterministically cleaned by
  `scripts/data/clean_official_knowledge_snapshots.py`.
- On `2026-07-22`, the cleaned snapshots were transformed into retrieval-focused operational
  summaries by `scripts/data/upgrade_high_value_knowledge.py`; the pinned source URL,
  revision, retrieval date, and license remain embedded in every local file.
- Cleaning removes YAML front matter, HTML comments, Hugo shortcodes, and internal-site link
  wrappers, converts Setext headings, and removes generic navigation/resource-list noise.
- The current hashes identify cleaned local artifacts, not byte-identical upstream files.
  To refresh content, pin a new upstream revision, rerun the cleaner, update hashes, and rerun
  retrieval evaluation.

| Local file | Upstream source | Pinned commit | Retrieved | License |
| --- | --- | --- | --- | --- |
| `official_kubernetes_debug_pods.md` | `https://github.com/kubernetes/website/blob/c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/debug-application/debug-pods.md` | `c3317651dc19ef683c5c4463bb6bf0602c0bf364` | 2026-07-21 | CC BY 4.0 |
| `official_kubernetes_debug_services.md` | `https://github.com/kubernetes/website/blob/c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/debug-application/debug-service.md` | `c3317651dc19ef683c5c4463bb6bf0602c0bf364` | 2026-07-21 | CC BY 4.0 |
| `official_kubernetes_pod_failure_reason.md` | `https://github.com/kubernetes/website/blob/c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/debug-application/determine-reason-pod-failure.md` | `c3317651dc19ef683c5c4463bb6bf0602c0bf364` | 2026-07-21 | CC BY 4.0 |
| `official_prometheus_alerting_practices.md` | `https://github.com/prometheus/docs/blob/47c3b182327d2832daadb00d0beacfcd802e4458/docs/practices/alerting.md` | `47c3b182327d2832daadb00d0beacfcd802e4458` | 2026-07-21 | Apache-2.0 |
| `official_prometheus_alerting_rules.md` | `https://github.com/prometheus/prometheus/blob/2cf323988931bd586a2ab25160e46bcace9398ae/docs/configuration/alerting_rules.md` | `2cf323988931bd586a2ab25160e46bcace9398ae` | 2026-07-21 | Apache-2.0 |
| `official_redis_clients.md` | `https://github.com/redis/docs/blob/36a9e2dbb407116f2a9d46d0f600cebdf8e4be68/content/develop/reference/clients.md` | `36a9e2dbb407116f2a9d46d0f600cebdf8e4be68` | 2026-07-21 | CC BY-NC-SA 4.0 and upstream notices |
| `official_redis_latency.md` | `https://github.com/redis/docs/blob/36a9e2dbb407116f2a9d46d0f600cebdf8e4be68/content/operate/oss_and_stack/management/optimization/latency.md` | `36a9e2dbb407116f2a9d46d0f600cebdf8e4be68` | 2026-07-21 | CC BY-NC-SA 4.0 and upstream notices |
| `official_loki_troubleshoot_ingest.md` | `https://github.com/grafana/loki/blob/925c8c7c7c6feface41c5bef12c74f05c05e8c84/docs/sources/operations/troubleshooting/troubleshoot-ingest.md` | `925c8c7c7c6feface41c5bef12c74f05c05e8c84` | 2026-07-21 | Grafana documentation terms; upstream repository AGPL-3.0 |
| `official_loki_troubleshoot_query.md` | `https://github.com/grafana/loki/blob/925c8c7c7c6feface41c5bef12c74f05c05e8c84/docs/sources/shared/troubleshoot-query.md` | `925c8c7c7c6feface41c5bef12c74f05c05e8c84` | 2026-07-21 | Grafana documentation terms; upstream repository AGPL-3.0 |

## Current cleaned hashes

| Local file | SHA-256 |
| --- | --- |
| `official_kubernetes_debug_pods.md` | `9156066AF3CA6502B31C994984623E09B7AA32AC6F78616A84B31A4D270CE726` |
| `official_kubernetes_debug_services.md` | `446175634102FFC0969405D6EDEE8D6B5781C565F782A65CCC4C52E1CE0CF1FC` |
| `official_kubernetes_pod_failure_reason.md` | `B2001F435F7ACABBCA6ED7780C034CFBF7547E129D5A83494CF483695D071553` |
| `official_loki_troubleshoot_ingest.md` | `794DF9A0B8076DE9C56D1E3E8F2A764FF05DAE8DD14A0F8B8E31C6B2205A4DBA` |
| `official_loki_troubleshoot_query.md` | `3DB38106F35BEC46212EED0E482699513B0733FA32E9A5F9F8FAD3B8A6C7BB28` |
| `official_prometheus_alerting_practices.md` | `FAB486E2B8212B83C4F5754B597009CDCCF7EF2B86E33319B598B8908F3FFCE4` |
| `official_prometheus_alerting_rules.md` | `4ED214CDBFFD2ACB500787F3EB23549CA24FCEA531C533550C897B10E92C03D5` |
| `official_redis_clients.md` | `DF8693439741B956F0ED0A96DC5AAD90E1C2692E26454866FF1ACD67ABE6A600` |
| `official_redis_latency.md` | `EFBAE57099744C7EACFD270E4C6D0643C7DF563A9ACA78A9AB2BBC5F8790AC1C` |

## Distribution boundary

- Preserve upstream source and license attribution when redistributing a snapshot.
- Redis documentation has a non-commercial license condition and requires separate review
  before commercial redistribution.
- Local cleaning improves retrieval quality but does not establish that the snapshot is the
  latest upstream version.
