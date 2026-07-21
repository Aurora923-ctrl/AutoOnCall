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
| `official_kubernetes_debug_pods.md` | `AEAEA304C74B94C94CE8B37BBCE134F2C8B4B299F08BF5DB1E7D39F2F1473AC2` |
| `official_kubernetes_debug_services.md` | `93A5C2AFE62CB25F4A8F6731CB982F862F851C94C1FD6B0224EB0295E9494A59` |
| `official_kubernetes_pod_failure_reason.md` | `B90A57B0D65BC6EDD151332B9B0D96F9BF54148F572761C61BDEC5A264964DB5` |
| `official_loki_troubleshoot_ingest.md` | `BBBD4BBD2A9A2DA2D0325F272EDFF4F5A98D3BDCA79E3BF08F11FE818F6B0259` |
| `official_loki_troubleshoot_query.md` | `FA8B945779F4F27E82FEB2661FD53383BC116BD77652EFFCECBD96621DFAA49E` |
| `official_prometheus_alerting_practices.md` | `64D8860A7D87105FBC7AFC307A77279AD7786F8B3D3E82B81B23741E415DBAED` |
| `official_prometheus_alerting_rules.md` | `0EFF0107BC828AA94D0D8CDBE13224DBEF3A58AE84D2DEFFA0FA15C3C3010CF4` |
| `official_redis_clients.md` | `C51754B6F76146675640A99F4B9B53F34538A7FBE5DB3B39EB3D23CFE37D5997` |
| `official_redis_latency.md` | `1D5678EEEF9697F5817463B8EBC95AFB8359F95707C90C590D1D79C9A8B78B75` |

## Distribution boundary

- Preserve upstream source and license attribution when redistributing a snapshot.
- Redis documentation has a non-commercial license condition and requires separate review
  before commercial redistribution.
- Local cleaning improves retrieval quality but does not establish that the snapshot is the
  latest upstream version.
