# 消息队列积压诊断 Runbook

## 文档元数据

- 适用范围：Kafka、RabbitMQ 等出现 consumer lag、ready messages、oldest message age、消费失败或死信增长；重放必须具备幂等保护。
- Owner：消息平台与业务消费者 Owner
- 最后复核：2026-07-21
- 关联工单：`INC-MQ-023`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

固定积压窗口后，分别记录 backlog、oldest age、producer rate、consumer rate、partition/queue 分布和失败消息键。Kafka 重点查看 consumer group lag、partition热点与 rebalance；RabbitMQ 重点查看 ready/unacked、consumer utilization 和 redelivery。追平时间按 `backlog / max(consumer_rate - producer_rate, 1)` 估算，避免盲目扩容。

## 首轮证据清单

- backlog、consumer lag、最老消息年龄、生产/消费速率和失败率
- 按 topic、partition、queue、consumer group 和消息类型拆分
- 消费者重启、rebalance、处理耗时、下游依赖和 poison message
- 确认消息幂等、顺序和重放边界，避免追赶积压时产生重复处理

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
sum by (topic, consumergroup) (kafka_consumergroup_lag)
max by (queue) (rabbitmq_queue_messages_ready)
rate(consumer_processing_errors_total[5m])
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

- `rebalance、commit failed、poison message、dead letter`
- `consumer timeout、database timeout、rate limited、deserialization error`

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

1. 生产速率突增且消费稳定：评估临时容量和预计追平时间。
2. 单 partition 积压：检查 key 倾斜和热点。
3. 失败集中同一消息：隔离 poison message 并保留内容摘要。
4. 消费者受下游限制：禁止盲目扩容消费者。

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：扩容消费者、修改并发、跳过或重放消息、调整保留期或移动死信。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚与恢复判定

隔离 poison message、DLQ、增加消费者、跳过或重放消息都必须记录消息范围、幂等保护、顺序要求、速率上限和审批人。若重复处理、rebalance、下游错误、数据库连接或 oldest age恶化，立即停止重放并恢复原消费并发。恢复需要净消费速率持续高于生产速率，oldest age回到 SLO，失败消息已隔离且业务幂等校验通过。Owner 将 offset/queue 范围、速率和 `INC-MQ-023` 写入审计记录。

## 长期行动项

- 监控 lag 斜率和预计追平时间
- 建立 poison message 隔离与审计流程
- 为重放设置速率、幂等和顺序保护

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
