# Negative Boundary Cases

These cases are for interview explanation and regression design. They show that AutoOnCall does not force a confident RCA when the evidence set is incomplete.

## Runbook Missing -> needs_human

Scenario:

- Primary domain evidence exists.
- Symptom evidence exists.
- Runbook retrieval returns no trusted answer, and no history ticket matches.

Expected behavior:

- Report status is `needs_human` if the core facts are plausible but remediation reference is missing.
- Report lists missing evidence: trusted Runbook or history-ticket reference.
- Report keeps confirmed facts separate from inferred root cause.
- Report caps confidence and recommends manual operator review before applying remediation.

Interview wording:

```text
The system can still summarize what Redis/MySQL/K8s evidence says, but it will not pretend the remediation is grounded. It explicitly marks the report as needing human review because the operational reference is missing.
```

## K8s RBAC Denied -> degraded

Scenario:

- `query_k8s_status` fails because the Kubernetes API denies pod/event access.
- Logs or metrics may still show restarts, OOM-like messages, or availability loss.
- No pod status or event evidence is available.

Expected behavior:

- Report status is `degraded`, not `completed`.
- Failed tool appears in the report and eval summary.
- Metrics/logs are treated as symptom evidence, not as a replacement for K8s domain evidence.
- Confidence is capped and the report recommends checking pod events, previous container state, memory limits, and recent deploys.

Interview wording:

```text
Metrics can show impact, but they cannot prove the pod event. If the K8s tool fails, the report downgrades and asks for manual verification instead of inventing a live K8s RCA.
```

## Why This Matters

These negative cases are the strongest answer to "why should I trust this Agent?" The trust comes not from always giving an answer, but from knowing when the answer is under-evidenced.
