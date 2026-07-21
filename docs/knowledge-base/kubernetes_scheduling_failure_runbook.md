# Kubernetes 调度失败 Runbook

## 适用范围

适用于 Pod 长时间 Pending、`FailedScheduling`、`Insufficient cpu`、资源不足、污点不容忍、亲和性冲突和 PVC
绑定失败。Owner 为 Kubernetes 平台与工作负载 Owner。

## 首轮证据

```bash
kubectl describe pod <pod>
kubectl get events --field-selector involvedObject.name=<pod>
kubectl get nodes -o wide
kubectl describe node <node>
```

记录 scheduler 事件、requests/limits、nodeSelector、affinity、topology spread、taints、
tolerations、PriorityClass、配额、PVC 和可用节点容量。

## 判断路径

`Insufficient cpu/memory` 时比较 requests 与可调度容量；不要只看实时使用率。taint 或 affinity
冲突时核对部署策略。PVC 未绑定时升级存储平台。大量低优先级 Pod 被抢占时检查 PriorityClass
和容量规划。

## 处置与审批

修改 requests、affinity、taints、配额、优先级、扩容节点或删除 Pod 都需要人工审批。先使用
单工作负载 canary，避免放宽约束后把 Pod 调度到不合规节点。

## 回滚与恢复

若错误节点、跨区成本、资源争抢或稳定性恶化，应回滚。恢复要求 Pod 成功调度并 Ready，
节点 headroom、拓扑和存储约束符合预期。
