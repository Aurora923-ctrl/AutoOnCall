# AutoOnCall Interview Eval Summary

## Rollup

- Status: `passed`
- Generated at: `2026-07-07T12:39:15.735366+00:00`
- Scope: interview-facing rollup of adapter verification, live AIOps golden eval, and standalone RAG retrieval eval.

## Module Results

| Module | Result | Pass rate | Notes |
| --- | --- | ---: | --- |
| Full stack adapter verification | `7/7 passed` | 100% | mock_fallback_detected=False |
| Live AIOps golden eval | `16/16 passed` | 100% | passed |
| RAG retrieval eval | `30/30 passed` | 100% | recall@3=100%, citation=100% |
| Safe-change eval | `9/9 passed` | 100% | passed |
| Replanner eval | `4/4 passed` | 100% | passed |

## Portfolio Chains

| Chain | Status | Evidence mode | Required signals |
| --- | --- | --- | --- |
| Redis maxclients | `PASS` | `live_adapter` | required_live_sources_hit=True; evidence_sufficiency_hit=True; runtime_vs_incident_boundary_hit=True; approval_boundary_hit=True |
| MySQL slow query | `PASS` | `live_adapter` | required_live_sources_hit=True; evidence_sufficiency_hit=True; runtime_vs_incident_boundary_hit=True; approval_boundary_hit=True |
| K8s CrashLoop/OOMKilled | `PASS` | `offline_fixture` | offline golden regression only |

## RAG Snapshot

- RAG eval: `30/30 passed`
- recall@3: `100%`
- strict recall@3: `100%`
- MRR: `1.00`
- citation coverage: `100%`
- no-answer rejection: `100%`
- confusion case pass: `100%`

## Conclusion Alignment

- conclusion_alignment_rate: `6/6 (100%)`
- Scope: Redis/MySQL main chains; fields are `root_cause`, `key_findings`, and `remediation_suggestion`.

| Case | Field | Status | Evidence links | Citation count |
| --- | --- | --- | --- | ---: |
| `redis_maxclients_timeout` | `root_cause` | `aligned` | evd-e63e5466471c466dba24d145454a2ab0, evd-3013606a40d040b6a38d3d12b39331ec (+3 more) | 2 |
| `redis_maxclients_timeout` | `key_findings` | `aligned` | evd-3013606a40d040b6a38d3d12b39331ec, evd-44136d169ad6456b88769b9717779575 (+2 more) | 2 |
| `redis_maxclients_timeout` | `remediation_suggestion` | `aligned` | evd-4e50e785cec440308db04ee529e4d1a1, evd-ec9d5f160df749cf9e6a0770eea49468 | 2 |
| `mysql_slow_query_latency` | `root_cause` | `aligned` | evd-a4f792e1c3154f54a3fcb990d657bb74, evd-31b48c6b16a0483291a57c5cd2e050c3 (+3 more) | 2 |
| `mysql_slow_query_latency` | `key_findings` | `aligned` | evd-31b48c6b16a0483291a57c5cd2e050c3, evd-a4f792e1c3154f54a3fcb990d657bb74 (+3 more) | 2 |
| `mysql_slow_query_latency` | `remediation_suggestion` | `aligned` | evd-a7fd3e9201364042b4142911c331d7a1, evd-5b3d6e46dcdd43fea59a5b0306d32590 | 2 |

## Milvus Multi-Source Snapshot

- Status: `passed`
- Inserted chunks: `18`
- Probe pass rate: `6/6`
- Source files: `mysql_slow_query_postmortem.pdf, payment_wiki.html, redis_capacity_wiki.html, redis_postmortem.pdf, tickets.csv, tickets.xlsx`

## Adapter Snapshot

- Adapter verification: `passed`
- Data sources: `cmdb, deploy_history, loki, mysql, prometheus, redis_info, ticket_api`
- mock_fallback_detected: `False`
- missing_sources: `[]`
- failed_tools: `[]`

## Interview Boundaries

- Redis/MySQL are live adapter golden chains backed by the local Docker stack.
- RAG eval is shown from its own retrieval summary, not from the --skip-rag AIOps run.
- K8s CrashLoop/OOMKilled is an offline golden regression case in the default interview.
- Conclusion alignment is conclusion-level grounding, not full-sentence fact checking.

## Source Artifacts

- `logs/live_golden_eval_summary_current.md`: live AIOps run; usually uses `--skip-rag`.
- `logs/rag_eval_summary_current.md`: standalone RAG retrieval result.
- `logs/milvus_multisource_verification.md`: Milvus storage proof for PDF/HTML/CSV/XLSX chunks.
- `logs/full_stack_adapter_verification.json`: real adapter source proof.
