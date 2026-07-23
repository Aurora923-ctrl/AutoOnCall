# CPU 使用率过高诊断 Runbook

## 文档元数据

- 适用范围：适用于 HighCPUUsage 告警：虚拟机、容器、Kubernetes 工作负载及应用进程出现持续 CPU 压力，并伴随延迟、错误率、队列或吞吐异常。
- Owner：运行平台与服务 Owner
- 最后复核：2026-07-21
- 关联工单：`OPS-CPU-RUNBOOK`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

固定 CPU 事故窗口后，先按 `cluster/namespace/workload/pod/container` 定位压力范围，再把 CPU 核数、throttling、run queue、QPS、P95 和错误率对齐到同一发布版本。若只有单 Pod 或单线程异常，保留线程栈与 profiler 摘要；若所有副本随流量同比增长，优先验证容量与重试放大。当前 CPU 指标只能证明资源压力，不能单独证明代码热点。

## 首轮证据清单

- CPU utilization、load average、CPU throttling、run queue 和上下文切换
- 按实例、Pod、进程、线程、endpoint、release 拆分的 P95/P99、QPS 与错误率
- 线程栈、火焰图或 profiler 摘要，以及同窗口发布和定时任务记录

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="<namespace>"}[5m]))
sum by (pod) (rate(container_cpu_cfs_throttled_seconds_total{namespace="<namespace>"}[5m]))
histogram_quantile(0.95, sum by (le, service) (rate(http_request_duration_seconds_bucket[5m])))
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

- `repeated stack、busy loop、timeout、retry exhausted、lock contention`
- `GC overhead、slow query、pool waiting 或 downstream timeout`

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

1. 仅单进程或少量线程接近满核：保留线程栈与 profiling，排查死循环、锁竞争和热点函数。
2. CPU 与流量同步增长：核对容量基线、重试预算和下游余量，避免把压力转移到数据库或缓存。
3. CPU 周期性升高：关联定时任务、补偿任务、批处理和任务互斥。
4. CPU 是在慢SQL、慢查询、依赖超时或队列增长之后升高：把 CPU 视为伴随现象并转入对应 Runbook。

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：限流、扩容、回滚、终止进程、调整线程池或重启实例。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚与恢复判定

CPU 处置必须以 canary 和对照组为基准。若 canary 的 5xx、P95/P99、请求丢弃、throttling、下游连接数或可用副本数任一恶化，立即停止扩容、限流、参数调整或进程操作，并按审批记录恢复原副本数、原配置或原版本。恢复需要 CPU 与 throttling回到事件前容量区间，业务延迟和错误率恢复，线程栈不再出现同一热点，并持续观察至少 30 分钟或一个业务高峰。Owner 记录验证查询、时间窗口和 `OPS-CPU-RUNBOOK`。

## 长期行动项

- 建立 endpoint、release 和线程级 profiling 基线
- 把 CPU、throttling、QPS、重试和依赖等待放在同一看板
- 为定时任务增加互斥、超时、并发预算和错峰策略

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
