# 服务慢响应与超时诊断 Runbook

## 文档元数据

- 适用范围：P95/P99 超过 SLO、请求排队、超时、吞吐下降或用户感知明显变慢，包括外部API超时、缓存穿透和数据库连接池等待。
- Owner：服务 Owner；涉及数据库时升级 DBA
- 最后复核：2026-07-21
- 关联工单：`OPS-LATENCY-RUNBOOK`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

固定延迟窗口后，用 trace 将总耗时拆成应用执行、数据库、缓存、外部 API、DNS/TLS、线程池排队和序列化，并按 route、release 与实例对比 P50/P95/P99。pool_waiting 与SQL hold time 同升时优先慢 SQL 或锁；线程队列增长且下游 span 变长时优先下游阻塞。资源指标只能解释伴随压力，不能替代分段耗时证据。

## 首轮证据清单

- P50/P95/P99、QPS、错误率、队列长度、线程池和数据库连接池等待
- trace 中应用、数据库、缓存、外部 API、DNS/TLS 和序列化分段耗时
- 数据库慢查询、SQL digest、锁等待、慢命令、最近发布、配置和流量变化

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
histogram_quantile(0.95, sum by (le, service, route) (rate(http_request_duration_seconds_bucket[5m])))
sum by (service) (application_db_pool_waiting)
sum by (downstream) (rate(client_request_duration_seconds_sum[5m]))
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

- `slow query、pool acquire timeout、lock wait、upstream timeout`
- `retry exhausted、queue full、task rejected、cache miss storm`

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

1. 数据库慢查询、SQL digest、连接持有和 pool_waiting 同时升高：只读获取 EXPLAIN，转入慢 SQL 或锁等待 Runbook。
2. 外部 API 慢：拆分 DNS、connect、TLS、first-byte 和 read 阶段。
3. 缓存命中率下降且数据库 QPS 上升：检查批量失效、穿透、热点 key 和 TTL。
4. 线程池队列增长：获取线程栈并确认阻塞点，禁止默认扩大线程池。

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：添加索引、改写 SQL、终止会话、调整连接池或线程池、缓存、超时、限流、降级或回滚。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚与恢复判定

索引、SQL、连接池、线程池、缓存、超时、限流、降级或版本回滚都必须先验证一个 canary。若 P95/P99、错误率、队列、锁等待、缓存穿透、下游错误或连接数恶化，立即恢复原配置或版本。恢复需要目标 route 延迟回到 SLO，pool_waiting、慢查询、线程队列和下游超时恢复，吞吐无异常下降，并完成端到端业务校验。Owner 记录 trace 样本、查询时间和 `OPS-LATENCY-RUNBOOK` 的观察结论。

## 长期行动项

- 建立 endpoint、release 和 downstream 延迟预算
- 为数据库慢查询、SQL digest、连接持有和线程阻塞建立回归
- 统一重试、线程池与连接池并发预算

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
