# 内存压力、OOM 与 GC 诊断 Runbook

## 文档元数据

- 适用范围：容器、JVM、Python/Go 服务或主机进程出现 working set 增长、OOMKilled、频繁 GC 或 swap 压力。
- Owner：运行平台与服务 Owner
- 最后复核：2026-07-21
- 关联工单：`OPS-MEMORY-RUNBOOK`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

固定内存事故窗口后，区分容器 working set、RSS、JVM heap、节点可用内存与 swap，并保存 OOMKilled、kernel OOM、GC pause 和 restart 时间线。Full GC 后仍持续增长更支持泄漏；流量下降后内存回落更支持负载或批量对象。heap dump、pprof 和 core 文件可能带来 IO、容量和敏感数据风险，必须先估算大小、脱敏和保留位置。

## 首轮证据清单

- RSS、working set、容器 limit、swap、OOM kill、restart count
- JVM heap、metaspace、GC 次数、暂停时长和 GC 后存活量
- 内存增长斜率、对象分配、缓存体积、流量和批量请求大小

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
container_memory_working_set_bytes / container_spec_memory_limit_bytes
increase(kube_pod_container_status_restarts_total[30m])
rate(jvm_gc_pause_seconds_sum[5m]) / rate(jvm_gc_pause_seconds_count[5m])
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

- `OutOfMemoryError、OOMKilled、GC overhead、allocation failure`
- `heap dump failure、memory limit exceeded、kernel oom`

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

1. Full GC 后占用仍持续增长：优先怀疑泄漏，保留 heap/profile 再提出重启计划。
2. 内存随流量突增且流量下降后回收：检查批量大小、序列化、大对象和背压。
3. 缓存体积增长或 TTL 缺失：评估穿透和数据库冲击，禁止直接清空缓存。
4. 容器 limit 或 JVM 参数不匹配：基于容量和 GC 证据制定 canary 配置变更。

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：生成 dump、重启、扩容、限流、调整 JVM 参数、容器 limit 或缓存策略。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚与恢复判定

dump、重启、扩容、JVM 参数、容器 limit 或缓存策略变更必须经审批并从 canary 开始。若 GC pause、OOM 循环、磁盘占用、错误率、P95 或数据库 QPS 恶化，立即恢复原参数、limit 或版本，并停止继续采样。恢复需要 working set 和 GC 后存活量回到基线，无新增 OOMKilled，核心接口和数据一致性验证通过，且 30 分钟内内存斜率不再异常。Owner记录 dump hash、存储位置、删除期限和 `OPS-MEMORY-RUNBOOK`。

## 长期行动项

- 建立 GC 后存活量和内存增长斜率告警
- 对上传、批处理和缓存设置大小上限
- 建立 dump 脱敏、保留和清理规范

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
