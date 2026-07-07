# AutoOnCall 5-Minute Interview Demo

This is the default campus-recruiting demo path. The goal is not to show many
containers; it is to show how an Agent is engineered with data-source boundaries,
tool contracts, evidence sufficiency, approvals, reports, feedback, and eval.

## Container Boundary

Default live stack:

- MySQL
- Redis
- metrics-exporter
- Prometheus
- Loki
- loki-log-emitter

Milvus/RAG is a bonus path. Start it separately with `make up && make upload`
only when you want to discuss citation-grounded Runbook QA.

Do not restore Grafana, Alertmanager, Tempo, Jaeger, Redpanda, or a K8s mock for
the default interview flow.

## Fixed Command Order

```powershell
make interview-up
make sandbox-verify
.venv\Scripts\python.exe scripts\eval\eval_cases.py `
  --cases eval\cases.yaml `
  --env-file deploy\sandbox.env `
  --report-path logs\live_golden_eval_reports.db `
  --summary-json logs\live_golden_eval_summary_current.json `
  --summary-md logs\live_golden_eval_summary_current.md `
  --skip-rag
.venv\Scripts\python.exe scripts\eval\eval_rag_cases.py `
  --cases eval\rag_cases.yaml `
  --docs-dir aiops-docs `
  --summary-json logs\rag_eval_summary_current.json `
  --summary-md logs\rag_eval_summary_current.md
.venv\Scripts\python.exe scripts\eval\build_interview_summary.py `
  --summary-json logs\interview_eval_summary.json `
  --summary-md logs\interview_eval_summary.md
```

Then open:

- `logs/interview_eval_summary.md`
- `logs/live_golden_eval_summary_current.md` only when the interviewer asks for AIOps details
- `logs/rag_eval_summary_current.md` only when the interviewer asks for RAG details
- one Redis or MySQL report from the generated report database or web UI
- `docs/negative-boundary-cases.md` if the interviewer asks how the system behaves when evidence is incomplete

## 5-Minute Talk Track

0:00-0:40: Project framing

AutoOnCall is not a chatbot wrapper. It turns an incident into a controlled
diagnosis workflow: planner, tool execution, evidence, replanning, report,
approval boundary, and eval.

0:40-1:40: Live container proof

Run `make sandbox-verify`. Point out that Redis, MySQL, Prometheus, Loki, and
ticket/service/deploy context return real adapter-backed sources. The important
claim is `mock_fallback_detected=false`.

1:40-3:10: Redis/MySQL golden chains

Run the eval command with `--env-file deploy\sandbox.env --skip-rag`. Explain:

- Redis/MySQL are live adapter golden chains.
- K8s is an offline golden regression case, not live container-backed.
- The live AIOps summary intentionally skips RAG; use `logs/interview_eval_summary.md`
  as the single rollup and `logs/rag_eval_summary_current.md` for standalone RAG.
- `runtime_vs_incident_boundary_hit=true` proves the report distinguishes current
  runtime from replay incident-window evidence.
- `evidence_sufficiency_hit=true` proves completed reports require primary
  domain evidence, symptom evidence, and Runbook or ticket context.
- `approval_boundary_hit=true` proves diagnosis can proceed read-only, while
  remediation changes still require approval.

3:10-4:20: Evidence walkthrough

For Redis, show `Redis Evidence Timeline`: incident evidence key, evidence hash,
timeline stream, hotkey context, and live runtime.

For MySQL, show `MySQL Evidence Chain`: slow SQL, connection-pool wait, and user
impact.

In the report, keep the audience on the first nine sections. They read like a
real OnCall incident review draft. Use the appendices only when asked to inspect
the ToolCall table, Evidence matrix, Trace summary, or Runbook references.

4:20-5:00: Engineering constraint

Close with the system boundary: tools are read-only by default, evidence records
carry fact/inference/uncertainty, production write actions are never executed
automatically, and eval prevents the core chains from silently regressing.

Also mention the negative boundary: if evidence is incomplete, reports are
downgraded to `incomplete`, `degraded`, or `needs_human` instead of pretending to
be certain. Operator feedback can then classify the miss into an eval draft,
RAG document gap, tool gap, or report-template issue.

For concrete negative examples, use `docs/negative-boundary-cases.md`: Runbook
missing becomes `needs_human`; K8s RBAC denied becomes `degraded`.

## Conclusion Alignment Wording

Use this sentence if asked how report conclusions are grounded:

AutoOnCall does not claim full-sentence fact checking. It performs
conclusion-level alignment: `root_cause`, `key_findings`, and
`remediation_suggestion` must link back to an `evidence_id` or RAG citation.
If that link is missing, the report is downgraded to `needs_human`.

## Honest K8s Wording

Use this sentence if asked:

Redis and MySQL are live adapter golden chains backed by the local Docker stack.
K8s CrashLoop/OOMKilled is currently an offline golden regression case. I did
not add a fake live K8s container because the interview default should stay
small and honest.
