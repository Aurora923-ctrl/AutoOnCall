<!-- AutoOnCall retrieval snapshot
Upstream: https://github.com/kubernetes/website/blob/c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/debug-application/debug-pods.md
Upstream revision: c3317651dc19ef683c5c4463bb6bf0602c0bf364
Retrieved: 2026-07-21
License: CC BY 4.0
Transformation: retrieval-focused operational summary; upstream attribution preserved
-->

# Kubernetes 官方 Pod 调试 - RAG 操作快照

## 适用范围

Pod Pending、CrashLoopBackOff、ImagePull、容器启动失败、重启和终止原因诊断。

本快照面向 AutoOnCall 事故诊断，只保留可用于分流、证据采集和风险判断的上游知识。需要完整
参数、版本差异或边缘行为时，应回到上游固定 revision 核对。

Owner 为 Kubernetes Platform 与当前 Incident 服务 Owner；最后复核时间为 2026-07-21；
适用版本以 Upstream revision 为准。关联问题需在内部 Incident 或变更工单中记录本快照版本。

## 最小证据集

- kubectl describe pod 的状态、容器状态和 Events
- kubectl logs --previous 与当前容器日志
- 资源 requests/limits、探针、镜像和节点状态

证据必须来自同一 Incident 时间窗口，并与健康实例或事件前基线比较。

## 可执行查询与判据

```bash
kubectl get pod <pod> -n <namespace> -o wide
kubectl describe pod <pod> -n <namespace>
kubectl logs <pod> -n <namespace> -c <container> --previous --since=30m
kubectl get pod <pod> -n <namespace> -o jsonpath='{range .status.containerStatuses[*]}{.name}{"\t"}{.state}{"\t"}{.lastState}{"\n"}{end}'
```
判据：Pending 必须有 scheduler Event；CrashLoop 必须保留当前与 previous 日志；OOM 需要 reason、limit 和节点内存同时支持。命令均为只读，输出记录 namespace、pod UID、resourceVersion 和采集时间。关联工单：`KB-K8S-POD-DEBUG`。

## 诊断工作流

1. 先确认 Pod phase 与每个 container state。
2. Pending 优先读取 scheduler Events。
3. Waiting 检查 reason、镜像、Secret、ConfigMap 和探针。
4. Terminated 检查 exit code、signal、OOMKilled 和 termination message。
5. 仅特定节点失败时比较节点条件、运行时和挂载。

官方文档提供产品行为和排查方法，不证明当前 Incident 的根因。历史经验、单条日志和当前
健康检查不能替代同窗口证据链。本快照的判定对象是“Pod Pending、CrashLoopBackOff、ImagePull、容器启动失败、重启和终止原因诊断。”。
形成结论前至少完成“先确认 Pod phase 与每个 container state。”，并记录支持证据、反证、缺失证据和置信度；
无法区分时继续采集只读证据，不通过生产写操作试错。

## AutoOnCall 审批边界

删除 Pod、修改资源、探针、镜像、调度约束或扩容节点均为生产变更，需要内部审批。

变更计划必须包含 approver、canary 范围、验证查询、观察时长和 rollback 条件。Agent 只生成
只读查询、证据摘要、候选假设和变更计划，不自动执行生产写操作。

## 恢复验证

恢复结论需要同时验证原始错误消失、用户侧或调用侧恢复、产品组件指标回到基线，并在约定
观察窗口内无复发。对本主题至少复查：kubectl describe pod 的状态、容器状态和 Events；kubectl logs --previous 与当前容器日志。每项验证都要保留查询时间、
筛选条件、结果摘要和 Owner。若 canary 未优于对照组，或错误率、延迟、资源消耗继续恶化，
应按已审批计划 rollback，不能把一次成功探测当作稳定恢复。

## 引用信息

- Source URL：`https://github.com/kubernetes/website/blob/c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/debug-application/debug-pods.md`
- Upstream revision：`c3317651dc19ef683c5c4463bb6bf0602c0bf364`
- License：CC BY 4.0
- Snapshot updated：2026-07-21
