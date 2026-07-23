# MySQL 锁等待与死锁诊断 Runbook

## 文档元数据

- 适用范围：lock wait timeout、deadlock、长事务、metadata lock、连接占用或 pool waiting。
- Owner：DBA 与服务 Owner
- 最后复核：2026-07-21
- 关联工单：`INC-MYSQL-LOCK-017`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

固定锁等待窗口后，通过只读 `performance_schema.data_lock_waits`、`data_locks`、`events_transactions_current` 和 processlist 构建 blocker -> waiter 锁图，记录事务年龄、锁类型、对象、SQL digest、连接池等待与复制延迟。pool_waiting 是影响信号，不是锁根因；只有锁图、事务时间和回退后的因果反转一致时才能确认阻塞事务。

## 首轮证据清单

- 等待事务、阻塞事务、SQL digest、事务开始时间和持锁对象
- lock wait、deadlock、active connections、pool waiting 和复制延迟
- 发布、DDL、批处理、定时任务和事务访问顺序

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
rate(mysql_global_status_innodb_row_lock_waits[5m])
mysql_global_status_threads_connected / mysql_global_variables_max_connections
rate(mysql_global_status_innodb_deadlocks[5m])
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

- `Lock wait timeout exceeded、Deadlock found`
- `metadata lock、waiting for table metadata lock、transaction rollback`

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

1. 单个长事务阻塞大量请求：确认业务状态、影响行和回滚成本。
2. DDL 引发 metadata lock：关联发布任务并停止继续扩散。
3. 不同更新顺序导致死锁：统一事务访问顺序。
4. pool waiting 仅是结果：必须以锁图和事务证据确认。

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：终止会话、回滚事务、停止批处理、修改 SQL/索引、DDL 或超时。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚与恢复判定

通知 Owner、暂停批任务、终止会话、回滚事务或调整 DDL 都属于受控数据库变更。审批记录必须包含 blocker 身份、未提交数据范围、复制影响、业务补偿和一致性检查。若 replica lag、rollback volume、数据差异、死锁或错误率增加，立即停止进一步终止并按 DBA 计划恢复。恢复需要锁等待图清空、pool_waiting 回到零、业务超时恢复、复制稳定，并完成关键账务校验。结果关联 `CR-LOCK-2026-017`。

## 长期行动项

- 监控长事务、锁等待图和 metadata lock
- 在发布前评估 DDL 锁风险
- 为关键事务建立固定访问顺序和超时预算

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
