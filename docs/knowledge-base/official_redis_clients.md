<!-- AutoOnCall retrieval snapshot
Upstream: https://github.com/redis/docs/blob/36a9e2dbb407116f2a9d46d0f600cebdf8e4be68/content/develop/reference/clients.md
Upstream revision: 36a9e2dbb407116f2a9d46d0f600cebdf8e4be68
Retrieved: 2026-07-21
License: CC BY-NC-SA 4.0 and upstream notices
Transformation: retrieval-focused operational summary; upstream attribution preserved
-->

# Redis 官方客户端连接与 maxclients - RAG 操作快照

## 适用范围

连接拒绝、maxclients、客户端超时、输出缓冲、连接空闲和连接所有者诊断。

本快照面向 AutoOnCall 事故诊断，只保留可用于分流、证据采集和风险判断的上游知识。需要完整
参数、版本差异或边缘行为时，应回到上游固定 revision 核对。

Owner 为 Redis Service Team 与当前 Incident 服务 Owner；最后复核时间为 2026-07-21；
适用版本以 Upstream revision 为准。关联问题需在内部 Incident 或变更工单中记录本快照版本。

## 最小证据集

- INFO clients 的 connected_clients、blocked_clients 和 maxclients
- rejected_connections、CLIENT LIST 聚合与操作系统文件描述符上限
- 应用连接池、重试、idle 连接、发布版本和错误率

证据必须来自同一 Incident 时间窗口，并与健康实例或事件前基线比较。

## 可执行查询与判据

```text
redis-cli -h <host> -p <port> INFO clients
redis-cli -h <host> -p <port> INFO stats
redis-cli -h <host> -p <port> --raw CLIENT LIST
```
计算 `effective_capacity=min(maxclients, os_fd_limit-reserved_fds)`，记录connected_clients、blocked_clients、rejected_connections 和按 user/lib-name/addr 聚合的连接所有者。CLIENT LIST 输出必须脱敏。关联工单：`KB-REDIS-CLIENTS`。

## 诊断工作流

1. 比较 connected_clients 与有效上限，而非只看配置值。
2. 按 addr、name、user、lib-name、idle 和 flags 聚合连接所有者。
3. rejected_connections 增长时关联应用 pool wait 与重试。
4. 检查输出缓冲异常客户端和 pub/sub、replica 类连接。
5. 确认容量压力是广泛增长还是单一 release/客户端导致。

官方文档提供产品行为和排查方法，不证明当前 Incident 的根因。历史经验、单条日志和当前
健康检查不能替代同窗口证据链。本快照的判定对象是“连接拒绝、maxclients、客户端超时、输出缓冲、连接空闲和连接所有者诊断。”。
形成结论前至少完成“比较 connected_clients 与有效上限，而非只看配置值。”，并记录支持证据、反证、缺失证据和置信度；
无法区分时继续采集只读证据，不通过生产写操作试错。

## AutoOnCall 审批边界

提高 maxclients、文件描述符、调整连接池、断开客户端或重启 Redis 都需要 Redis Owner 审批和 canary/回滚方案。

变更计划必须包含 approver、canary 范围、验证查询、观察时长和 rollback 条件。Agent 只生成
只读查询、证据摘要、候选假设和变更计划，不自动执行生产写操作。

## 恢复验证

恢复结论需要同时验证原始错误消失、用户侧或调用侧恢复、产品组件指标回到基线，并在约定
观察窗口内无复发。对本主题至少复查：INFO clients 的 connected_clients、blocked_clients 和 maxclients；rejected_connections、CLIENT LIST 聚合与操作系统文件描述符上限。每项验证都要保留查询时间、
筛选条件、结果摘要和 Owner。若 canary 未优于对照组，或错误率、延迟、资源消耗继续恶化，
应按已审批计划 rollback，不能把一次成功探测当作稳定恢复。

## 引用信息

- Source URL：`https://github.com/redis/docs/blob/36a9e2dbb407116f2a9d46d0f600cebdf8e4be68/content/develop/reference/clients.md`
- Upstream revision：`36a9e2dbb407116f2a9d46d0f600cebdf8e4be68`
- License：CC BY-NC-SA 4.0 and upstream notices
- Snapshot updated：2026-07-21
