# 消息队列积压 Runbook

## 适用范围

适用于 consumer lag、ready messages、oldest message age、消费失败和死信增长。适配 Kafka、
RabbitMQ 及同类消息系统。Owner 为消息平台与业务消费者团队。

## 首轮证据

- 查询 backlog、consumer lag、最老消息年龄、生产/消费速率和失败率；
- 按 topic、partition、queue、consumer group 和消息类型拆分；
- 检查消费者重启、rebalance、处理耗时、下游依赖和 poison message；
- 确认消息幂等、顺序和重放边界，避免追赶积压时产生重复处理。

## 判断路径

生产速率突增而消费能力稳定时，评估临时容量。单 partition 积压时检查 key 倾斜。消费失败
集中于同一消息时隔离 poison message。消费者受数据库或外部 API 限制时，不要盲目扩容消费
者。

## 处置与审批

扩容消费者、修改并发、跳过消息、重放、调整保留期或移动死信均需人工审批。计划应说明幂等
保证、顺序影响、最大追赶速率和下游容量。

## 回滚与恢复

若重复处理、下游过载、错误率或 rebalance 增加，应回滚。恢复要求 lag 和 oldest message age
持续下降，消费速率高于生产速率，死信稳定且业务校验通过。
