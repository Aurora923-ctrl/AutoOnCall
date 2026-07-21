# MySQL 锁等待与死锁 Runbook

## 适用范围

适用于 lock wait timeout、deadlock、长事务、metadata lock 和连接占用。Owner 为 DBA 与对应
服务团队。

## 首轮证据

- 捕获等待事务、阻塞事务、SQL digest、事务开始时间和持锁对象；
- 查询 lock wait、deadlock、active connections、pool waiting 和复制延迟；
- 关联发布、DDL、批处理和定时任务；
- 保留 `SHOW ENGINE INNODB STATUS` 或 performance_schema 只读快照。

## 判断路径

单个长事务阻塞大量请求时，确认业务状态和回滚成本。DDL 引发 metadata lock 时检查发布任务。
不同更新顺序导致死锁时修复事务访问顺序。连接池等待只是结果，不能替代锁证据。

## 处置与审批

终止会话、回滚事务、停止批处理、修改 SQL/索引或调整超时均需 DBA 和业务 Owner 审批。
计划必须包含事务影响、数据一致性、复制状态、回滚方式和验证查询。

## 回滚与恢复

若复制延迟、回滚量、错误率或数据校验恶化，应停止动作。恢复要求阻塞链消失、lock wait 和
pool waiting 回到基线、事务吞吐恢复，并完成业务一致性检查。
