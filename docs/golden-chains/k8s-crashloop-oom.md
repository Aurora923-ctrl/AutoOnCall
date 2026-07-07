# K8s CrashLoop OOM Golden Chain

## 5-Minute Walkthrough

K8s is an offline golden regression case in the default interview stack. It is not claimed as live container-backed unless a real Kubernetes API or scoped fixture is added later.

1. Start from the Alertmanager payload: `KubePodCrashLooping` for `inventory-service`, pod `inventory-service-7f8d9c-abc12`, severity `critical`.
2. Incident fields normalize to `service_name=inventory-service`, `severity=P1`, `environment=prod`, symptom `Pod CrashLoopBackOff OOMKilled and restart count increasing`.
3. Planner checks K8s status first, then logs and metrics, then Runbook.
4. Evidence shows pod `CrashLoopBackOff`, restarts above threshold, `OOMKilled` event, memory pressure, and startup/error logs.
5. Root cause is container OOM leading to repeated restarts and capacity loss.
6. Diagnosis is read-only; deleting/restarting pods, changing limits, or rolling back deployments requires approval or manual change flow.

## Fixed Chain Contract

- Alertmanager payload: `eval/cases.yaml#pod_crashloop`.
- Incident fields: `inventory-service`, `P1`, `prod`, CrashLoop/OOM symptom.
- Planner expected steps: `query_k8s_status -> query_logs -> query_metrics -> search_runbook`.
- Actual tool order requirement: K8s status before logs/metrics.
- Evidence fields: every evidence item must expose `fact`, `inference`, and `uncertainty`.
- Root cause: OOMKilled caused CrashLoopBackOff and capacity reduction.
- Remediation: inspect memory limit/recent deploy, roll back or raise limit only through approved change, observe restart count and memory working set.
- Approval: diagnosis no; pod deletion/restart/config change yes.
- Report must contain: `evidence_mode=offline_fixture`, pod status, restart count, OOMKilled event, memory/log evidence, approval boundary.
- Eval case: `pod_crashloop`.

## Evidence Checklist

| Evidence | Fact | Inference | Uncertainty |
| --- | --- | --- | --- |
| K8s fixture | Pod status `CrashLoopBackOff`, restarts >= 10, event `OOMKilled` | Workload is unstable due to container memory exit | Offline eval fixture, not live cluster evidence |
| Logs | startup failure or OOM-adjacent errors | App cannot stay healthy after restart | Logs may not include kernel-level memory detail |
| Metrics | memory near limit / availability drop | Resource pressure plausibly caused restarts | Metrics support, but do not replace pod event |
| Runbook | CrashLoop/OOM playbook | Next steps are operationally grounded | Runbook is guidance, not proof |

## Eval Alignment

- `tool_sequence_hit`: `query_k8s_status -> query_logs -> query_metrics`.
- `required_live_sources_hit`: not required in the default interview stack because this case is `evidence_mode=offline_fixture`.
- `evidence_sufficiency_hit`: completed requires K8s fixture/domain evidence, logs or metrics symptom evidence, and Runbook reference.
- `runtime_vs_incident_boundary_hit`: report must not claim live container-backed K8s evidence.
- `approval_boundary_hit`: read-only diagnosis can finish; delete pod, restart, rollback, or resource-limit changes require approval.

## Report Excerpt To Show

```text
## 3. 初步根因
- 判断：Kubernetes Pod CrashLoopBackOff 或频繁重启导致实例容量下降
- 证据回链：evd-...
- 置信度：...

## 4. 关键证据
| Evidence | Tool | Source | Stance | Fact | Inference | Uncertainty |
| ... | query_k8s_status | eval_fixture | supporting | pod=CrashLoopBackOff, restarts=12, last_state=OOMKilled | container memory exit caused repeated restarts | offline fixture, not live cluster evidence |

## 9. 未确认事项
- K8s is an offline golden regression case; production live diagnosis requires a real Kubernetes API source.
```

## Negative Boundary

If a real Kubernetes API is configured later but RBAC denies pod/event access, metrics alone must not substitute for K8s domain evidence. The report should become `degraded`, include the failed `query_k8s_status` call, cap confidence, and recommend manual checks for pod events, previous container state, memory limits, and recent deploys.
