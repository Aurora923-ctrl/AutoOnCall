<!-- AutoOnCall retrieval snapshot
Upstream: https://github.com/kubernetes/website/blob/c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/debug-application/determine-reason-pod-failure.md
Upstream revision: c3317651dc19ef683c5c4463bb6bf0602c0bf364
Retrieved: 2026-07-21
License: CC BY 4.0
Transformation: retrieval-focused operational summary; upstream attribution preserved
-->

# Kubernetes 官方容器终止原因 - RAG 操作快照

## 适用范围

需要从 exit code、signal、reason 和 termination message 判断容器失败原因。

本快照面向 AutoOnCall 事故诊断，只保留可用于分流、证据采集和风险判断的上游知识。需要完整
参数、版本差异或边缘行为时，应回到上游固定 revision 核对。

Owner 为 Kubernetes Platform 与当前 Incident 服务 Owner；最后复核时间为 2026-07-21；
适用版本以 Upstream revision 为准。关联问题需在内部 Incident 或变更工单中记录本快照版本。

## 最小证据集

- containerStatuses.state.terminated 与 lastState.terminated
- exitCode、signal、reason、startedAt、finishedAt
- /dev/termination-log 或 terminationMessagePolicy 产生的消息

证据必须来自同一 Incident 时间窗口，并与健康实例或事件前基线比较。

## 可执行查询与判据

```bash
kubectl get pod <pod> -n <namespace> -o jsonpath='{range .status.containerStatuses[*]}{.name}{"\t"}{.lastState.terminated.reason}{"\t"}{.lastState.terminated.exitCode}{"\t"}{.lastState.terminated.finishedAt}{"\n"}{end}'
kubectl logs <pod> -n <namespace> -c <container> --previous --since=30m
```
判据：exit 137 只有与 OOMKilled、limit 或节点压力一致时才支持内存假设；SIGTERM 需要关联 rollout、探针或驱逐事件。termination message 是摘要，不能替代完整日志。关联工单：`KB-K8S-TERMINATION-REASON`。

## 诊断工作流

1. 优先读取 lastState，避免重启后丢失上一次原因。
2. exit 137 结合 OOMKilled 与节点内存证据判断，不只凭数字。
3. 非零业务退出码关联应用日志和发布版本。
4. 信号退出关联 kubelet、探针、驱逐和人工操作记录。
5. termination message 只作摘要，仍需关联完整日志。

官方文档提供产品行为和排查方法，不证明当前 Incident 的根因。历史经验、单条日志和当前
健康检查不能替代同窗口证据链。本快照的判定对象是“需要从 exit code、signal、reason 和 termination message 判断容器失败原因。”。
形成结论前至少完成“优先读取 lastState，避免重启后丢失上一次原因。”，并记录支持证据、反证、缺失证据和置信度；
无法区分时继续采集只读证据，不通过生产写操作试错。

## AutoOnCall 审批边界

修改 terminationMessagePolicy、探针、资源或重启策略属于部署变更，需要服务 Owner 审批。

变更计划必须包含 approver、canary 范围、验证查询、观察时长和 rollback 条件。Agent 只生成
只读查询、证据摘要、候选假设和变更计划，不自动执行生产写操作。

## 恢复验证

恢复结论需要同时验证原始错误消失、用户侧或调用侧恢复、产品组件指标回到基线，并在约定
观察窗口内无复发。对本主题至少复查：containerStatuses.state.terminated 与 lastState.terminated；exitCode、signal、reason、startedAt、finishedAt。每项验证都要保留查询时间、
筛选条件、结果摘要和 Owner。若 canary 未优于对照组，或错误率、延迟、资源消耗继续恶化，
应按已审批计划 rollback，不能把一次成功探测当作稳定恢复。

## 引用信息

- Source URL：`https://github.com/kubernetes/website/blob/c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/debug-application/determine-reason-pod-failure.md`
- Upstream revision：`c3317651dc19ef683c5c4463bb6bf0602c0bf364`
- License：CC BY 4.0
- Snapshot updated：2026-07-21
