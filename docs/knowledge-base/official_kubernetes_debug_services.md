<!-- AutoOnCall retrieval snapshot
Upstream: https://github.com/kubernetes/website/blob/c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/debug-application/debug-service.md
Upstream revision: c3317651dc19ef683c5c4463bb6bf0602c0bf364
Retrieved: 2026-07-21
License: CC BY 4.0
Transformation: retrieval-focused operational summary; upstream attribution preserved
-->

# Kubernetes 官方 Service 调试 - RAG 操作快照

## 适用范围

Service 无响应、EndpointSlice 为空、DNS 正常但连接失败、kube-proxy 或网络路径异常。

本快照面向 AutoOnCall 事故诊断，只保留可用于分流、证据采集和风险判断的上游知识。需要完整
参数、版本差异或边缘行为时，应回到上游固定 revision 核对。

Owner 为 Kubernetes Platform 与当前 Incident 服务 Owner；最后复核时间为 2026-07-21；
适用版本以 Upstream revision 为准。关联问题需在内部 Incident 或变更工单中记录本快照版本。

## 最小证据集

- Service selector、ports、targetPort 与 EndpointSlice
- Pod 内 DNS、ClusterIP、endpoint IP 和端口的分层探测
- NetworkPolicy、kube-proxy、CNI、节点和负载均衡事件

证据必须来自同一 Incident 时间窗口，并与健康实例或事件前基线比较。

## 可执行查询与判据

```bash
kubectl get svc <service> -n <namespace> -o yaml
kubectl get endpointslice -n <namespace> -l kubernetes.io/service-name=<service> -o wide
kubectl run netcheck --rm -i --restart=Never --image=curlimages/curl -- sh -c 'nslookup <service> && curl -sv --max-time 3 http://<service>:<port>/health'
```
判据：selector 必须命中 Ready Pod，EndpointSlice 的端口与 targetPort 一致；endpoint IP 成功但 ClusterIP 失败才优先 kube-proxy/CNI。临时探测 Pod 的创建需符合集群审批和清理策略。关联工单：`KB-K8S-SERVICE-DEBUG`。

## 诊断工作流

1. 确认 Service 存在且 selector 命中 Ready Pod。
2. 检查 EndpointSlice 地址、端口和 ready 条件。
3. 从 Pod 内依次验证 DNS、ClusterIP、endpoint IP。
4. 仅 ClusterIP 失败时检查 kube-proxy/CNI；endpoint 也失败时检查应用监听和策略。
5. External LoadBalancer 问题另查云负载均衡健康与安全规则。

官方文档提供产品行为和排查方法，不证明当前 Incident 的根因。历史经验、单条日志和当前
健康检查不能替代同窗口证据链。本快照的判定对象是“Service 无响应、EndpointSlice 为空、DNS 正常但连接失败、kube-proxy 或网络路径异常。”。
形成结论前至少完成“确认 Service 存在且 selector 命中 Ready Pod。”，并记录支持证据、反证、缺失证据和置信度；
无法区分时继续采集只读证据，不通过生产写操作试错。

## AutoOnCall 审批边界

修改 selector、端口、NetworkPolicy、kube-proxy、CNI 或负载均衡配置必须经平台 Owner 审批。

变更计划必须包含 approver、canary 范围、验证查询、观察时长和 rollback 条件。Agent 只生成
只读查询、证据摘要、候选假设和变更计划，不自动执行生产写操作。

## 恢复验证

恢复结论需要同时验证原始错误消失、用户侧或调用侧恢复、产品组件指标回到基线，并在约定
观察窗口内无复发。对本主题至少复查：Service selector、ports、targetPort 与 EndpointSlice；Pod 内 DNS、ClusterIP、endpoint IP 和端口的分层探测。每项验证都要保留查询时间、
筛选条件、结果摘要和 Owner。若 canary 未优于对照组，或错误率、延迟、资源消耗继续恶化，
应按已审批计划 rollback，不能把一次成功探测当作稳定恢复。

## 引用信息

- Source URL：`https://github.com/kubernetes/website/blob/c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/debug-application/debug-service.md`
- Upstream revision：`c3317651dc19ef683c5c4463bb6bf0602c0bf364`
- License：CC BY 4.0
- Snapshot updated：2026-07-21
