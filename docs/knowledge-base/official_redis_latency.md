<!-- AutoOnCall retrieval snapshot
Upstream: https://github.com/redis/docs/blob/36a9e2dbb407116f2a9d46d0f600cebdf8e4be68/content/operate/oss_and_stack/management/optimization/latency.md
Upstream revision: 36a9e2dbb407116f2a9d46d0f600cebdf8e4be68
Retrieved: 2026-07-21
License: CC BY-NC-SA 4.0 and upstream notices
Transformation: retrieval-focused operational summary; upstream attribution preserved
-->

# Redis 官方延迟诊断 - RAG 操作快照

## 适用范围

Redis 命令、fork、持久化、系统、网络、swap 或客户端行为造成的延迟尖峰。

本快照面向 AutoOnCall 事故诊断，只保留可用于分流、证据采集和风险判断的上游知识。需要完整
参数、版本差异或边缘行为时，应回到上游固定 revision 核对。

Owner 为 Redis Service Team 与当前 Incident 服务 Owner；最后复核时间为 2026-07-21；
适用版本以 Upstream revision 为准。关联问题需在内部 Incident 或变更工单中记录本快照版本。

## 最小证据集

- 应用端延迟、redis-cli --latency 与 intrinsic latency
- SLOWLOG、LATENCY LATEST/DOCTOR、commandstats
- latest_fork_usec、AOF/RDB、swap、CPU、网络 round-trip 和客户端连接模式

证据必须来自同一 Incident 时间窗口，并与健康实例或事件前基线比较。

## 可执行查询与判据

```text
redis-cli -h <host> -p <port> --latency-history -i 1
redis-cli -h <host> -p <port> SLOWLOG GET 32
redis-cli -h <host> -p <port> LATENCY LATEST
redis-cli -h <host> -p <port> INFO commandstats
```
先比较客户端 RTT 与 Redis intrinsic latency；再按慢命令、fork/AOF/RDB、swap/THP 和连接往返分流。生产 SLOWLOG 参数和 key 必须脱敏，不自动执行内核或持久化变更。关联工单：`KB-REDIS-LATENCY`。

## 诊断工作流

1. 先区分客户端网络延迟与 Redis 服务端处理延迟。
2. SLOWLOG 命中时检查命令复杂度、大 key 和 KEYS 类阻塞。
3. 延迟与 BGSAVE/BGREWRITEAOF 同步时检查 fork、内存和磁盘。
4. 检查 swap、透明大页和虚拟化基线，但不直接改内核。
5. 连接频繁建立或大量 round-trip 时优化客户端连接复用和 pipeline 方案。

官方文档提供产品行为和排查方法，不证明当前 Incident 的根因。历史经验、单条日志和当前
健康检查不能替代同窗口证据链。本快照的判定对象是“Redis 命令、fork、持久化、系统、网络、swap 或客户端行为造成的延迟尖峰。”。
形成结论前至少完成“先区分客户端网络延迟与 Redis 服务端处理延迟。”，并记录支持证据、反证、缺失证据和置信度；
无法区分时继续采集只读证据，不通过生产写操作试错。

## AutoOnCall 审批边界

禁用 THP、调整持久化、内核、实例、慢命令、客户端 pipeline 或重启均为审批变更；先保存基线和回滚条件。

变更计划必须包含 approver、canary 范围、验证查询、观察时长和 rollback 条件。Agent 只生成
只读查询、证据摘要、候选假设和变更计划，不自动执行生产写操作。

## 恢复验证

恢复结论需要同时验证原始错误消失、用户侧或调用侧恢复、产品组件指标回到基线，并在约定
观察窗口内无复发。对本主题至少复查：应用端延迟、redis-cli --latency 与 intrinsic latency；SLOWLOG、LATENCY LATEST/DOCTOR、commandstats。每项验证都要保留查询时间、
筛选条件、结果摘要和 Owner。若 canary 未优于对照组，或错误率、延迟、资源消耗继续恶化，
应按已审批计划 rollback，不能把一次成功探测当作稳定恢复。

## 引用信息

- Source URL：`https://github.com/redis/docs/blob/36a9e2dbb407116f2a9d46d0f600cebdf8e4be68/content/operate/oss_and_stack/management/optimization/latency.md`
- Upstream revision：`36a9e2dbb407116f2a9d46d0f600cebdf8e4be68`
- License：CC BY-NC-SA 4.0 and upstream notices
- Snapshot updated：2026-07-21
