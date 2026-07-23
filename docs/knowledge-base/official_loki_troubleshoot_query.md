<!-- AutoOnCall retrieval snapshot
Upstream: https://github.com/grafana/loki/blob/925c8c7c7c6feface41c5bef12c74f05c05e8c84/docs/sources/shared/troubleshoot-query.md
Upstream revision: 925c8c7c7c6feface41c5bef12c74f05c05e8c84
Retrieved: 2026-07-21
License: Grafana documentation terms; upstream repository AGPL-3.0
Transformation: retrieval-focused operational summary; upstream attribution preserved
-->

# Grafana Loki 官方查询故障 - RAG 操作快照

## 适用范围

LogQL 解析、查询限制、超时、并发、存储、认证或无数据问题。

本快照面向 AutoOnCall 事故诊断，只保留可用于分流、证据采集和风险判断的上游知识。需要完整
参数、版本差异或边缘行为时，应回到上游固定 revision 核对。

Owner 为 Observability Logging 与当前 Incident 服务 Owner；最后复核时间为 2026-07-21；
适用版本以 Upstream revision 为准。关联问题需在内部 Incident 或变更工单中记录本快照版本。

## 最小证据集

- HTTP status、错误消息、tenant、query hash、时间范围和 LogQL
- loki_request_duration_seconds、bytes processed、chunks scanned
- query-frontend、scheduler、querier、index gateway 和对象存储日志

证据必须来自同一 Incident 时间窗口，并与健康实例或事件前基线比较。

## 可执行查询与判据

```logql
{cluster="<cluster>", namespace="<namespace>"} |= "<error>" | json
sum by (status_code) (rate({component=~"query-frontend|querier"} | json [5m]))
```
记录 query hash、tenant、start/end、matcher、line filter、bytes 和 chunks。400 检查语法；429 检查 tenant 并发与队列；504 先缩短时间范围并前置精确 matcher；无数据要区分selector、retention、ingest delay 和索引就绪。关联工单：`KB-LOKI-QUERY`。

## 诊断工作流

1. 400 先检查语法、时间范围、label matcher 和限制。
2. 429 检查并发、队列和 tenant 限制。
3. 504 缩短时间范围、增加精确 matcher、提前 line filter 并查看 query stats。
4. 无数据时区分 selector、保留期、写入故障和索引未就绪。
5. 5xx 检查 index/chunk 存储、querier 和网络，不通过无限重试放大负载。

官方文档提供产品行为和排查方法，不证明当前 Incident 的根因。历史经验、单条日志和当前
健康检查不能替代同窗口证据链。本快照的判定对象是“LogQL 解析、查询限制、超时、并发、存储、认证或无数据问题。”。
形成结论前至少完成“400 先检查语法、时间范围、label matcher 和限制。”，并记录支持证据、反证、缺失证据和置信度；
无法区分时继续采集只读证据，不通过生产写操作试错。

## AutoOnCall 审批边界

提高 query timeout、parallelism、limits 或修改存储配置必须审批；先通过查询优化和 canary 验证资源影响。

变更计划必须包含 approver、canary 范围、验证查询、观察时长和 rollback 条件。Agent 只生成
只读查询、证据摘要、候选假设和变更计划，不自动执行生产写操作。

## 恢复验证

恢复结论需要同时验证原始错误消失、用户侧或调用侧恢复、产品组件指标回到基线，并在约定
观察窗口内无复发。对本主题至少复查：HTTP status、错误消息、tenant、query hash、时间范围和 LogQL；loki_request_duration_seconds、bytes processed、chunks scanned。每项验证都要保留查询时间、
筛选条件、结果摘要和 Owner。若 canary 未优于对照组，或错误率、延迟、资源消耗继续恶化，
应按已审批计划 rollback，不能把一次成功探测当作稳定恢复。

## 引用信息

- Source URL：`https://github.com/grafana/loki/blob/925c8c7c7c6feface41c5bef12c74f05c05e8c84/docs/sources/shared/troubleshoot-query.md`
- Upstream revision：`925c8c7c7c6feface41c5bef12c74f05c05e8c84`
- License：Grafana documentation terms; upstream repository AGPL-3.0
- Snapshot updated：2026-07-21
