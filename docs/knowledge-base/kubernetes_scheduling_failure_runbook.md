# Kubernetes 调度失败诊断 Runbook

## 文档元数据

- 适用范围：Pod 长时间 Pending、FailedScheduling、Insufficient cpu/memory、污点不容忍、亲和性冲突或 PVC 绑定失败。
- Owner：Kubernetes 平台与工作负载 Owner
- 最后复核：2026-07-21
- 关联工单：`INC-K8S-031`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

固定 Pending 窗口后，先保存 `kubectl describe pod` 的 FailedScheduling Events，再计算目标 Pod requests 与候选节点 allocatable 的差额，并逐项核对 taint/toleration、node affinity、topology spread、PVC 绑定和 PriorityClass。仅当事件和候选节点计算同时支持时，才把资源或调度约束列为主因；ImagePull 或应用 Crash 不属于调度失败。

## 首轮证据清单

- scheduler 事件、requests/limits、可调度节点容量和配额
- nodeSelector、affinity、topology spread、taints/tolerations、PriorityClass
- PVC、StorageClass、节点状态和 autoscaler 决策

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
sum by (namespace) (kube_pod_status_phase{phase="Pending"})
sum by (node) (kube_node_status_allocatable{resource=~"cpu|memory"})
increase(scheduler_schedule_attempts_total{result="unschedulable"}[15m])
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

- `FailedScheduling、Insufficient cpu、untolerated taint`
- `did not match affinity、unbound immediate PersistentVolumeClaims`

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

1. Insufficient cpu/memory：比较 requests 与可调度容量，不只看实时使用率。
2. taint 或 affinity 冲突：核对部署约束和合规边界。
3. PVC 未绑定：升级存储平台并检查拓扑。
4. 大量低优先级 Pod 被抢占：检查 PriorityClass 和容量规划。

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：修改 requests、affinity、taints、配额、优先级、扩容节点或删除 Pod。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚与恢复判定

requests、affinity、taint、PDB、PriorityClass、PVC 或节点池调整必须先作用于单个 canary 工作负载。若出现错误可用区放置、资源争用、驱逐、PDB 受损、成本异常或新 Pending，立即恢复原 workload spec 或节点池配置。恢复需要目标副本全部 Scheduled 且 Ready，FailedScheduling 不再增长，节点余量仍满足安全预算，批处理吞吐恢复，并观察至少一个调度周期。Owner 把事件快照、容量计算和 `CR-K8S-2026-031` 写入记录。

## 长期行动项

- 为 requests 与可调度容量建立看板
- 在 CI 校验 affinity、toleration 和 PVC 拓扑
- 演练节点池耗尽与 autoscaler 失败

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
