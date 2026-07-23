# 线程池耗尽与请求排队诊断 Runbook

## 文档元数据

- 适用范围：active threads 达上限、queue depth 增长、task rejected、请求堆积或线程长期阻塞；处置前必须核对下游容量。
- Owner：服务 Owner 与运行平台
- 最后复核：2026-07-21
- 关联工单：`INC-THREAD-008`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

固定线程池事故窗口后，记录 active、pool size、queue depth、rejected tasks 和任务等待时间，并采集两次间隔线程栈区分 CPU busy、锁等待、数据库等待、外部 API 和无界队列。仅扩大线程池可能把压力转移到数据库或下游；必须先证明阻塞点和并发预算。JVM 使用 `jcmd Thread.print`，其他运行时使用等价的只读 profile，并对敏感参数脱敏。

## 首轮证据清单

- active、pool size、queue depth、rejected tasks 和任务耗时
- 线程栈按锁、IO、数据库、外部 API 和 CPU 热点分类
- QPS、超时、重试、连接池等待和发布版本

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
application_thread_pool_active / application_thread_pool_max
rate(application_thread_pool_rejected_total[5m])
histogram_quantile(0.95, sum by (le, pool) (rate(application_task_duration_seconds_bucket[5m])))
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

- `RejectedExecutionException、task rejected、queue full`
- `pool acquire timeout、deadlock、blocked thread、upstream timeout`

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

1. 线程阻塞在数据库或外部 API：优先修复依赖等待。
2. CPU 满且 runnable 线程多：转入 CPU Runbook。
3. 线程数不高但队列增长：检查单任务耗时、串行锁和背压。
4. 仅某版本异常：比较线程模型和超时配置。

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：扩大线程池、调整队列、并发、拒绝策略、限流、超时或重启。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚与恢复判定

调整线程数、队列、超时、并发、隔离舱或重启实例必须从 canary 开始，并核对下游容量。若 rejected tasks、数据库连接、下游并发、CPU、错误率或队列等待恶化，立即恢复原线程池配置或版本。恢复需要队列持续下降、rejected tasks 归零、线程栈不再集中于同一阻塞点、P99 回到 SLO，且下游未出现压力转移。Owner 保存线程栈 hash、采集时间和 `CR-THREAD-2026-008` 的验证记录。

## 长期行动项

- 为每个线程池建立并发预算
- 压测验证拒绝策略和背压
- 将线程池、连接池和下游容量统一建模

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
