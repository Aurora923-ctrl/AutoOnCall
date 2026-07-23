<!-- AutoOnCall retrieval snapshot
Upstream: https://github.com/grafana/loki/blob/925c8c7c7c6feface41c5bef12c74f05c05e8c84/docs/sources/operations/troubleshooting/troubleshoot-ingest.md
Upstream revision: 925c8c7c7c6feface41c5bef12c74f05c05e8c84
Retrieved: 2026-07-21
License: Grafana documentation terms; upstream repository AGPL-3.0
Transformation: retrieval-focused operational summary; upstream attribution preserved
-->

# Grafana Loki 官方写入故障 - RAG 操作快照

## 适用范围

日志写入 429、验证失败、乱序、时间戳、流数量、存储或 distributor/ingester 故障。

本快照面向 AutoOnCall 事故诊断，只保留可用于分流、证据采集和风险判断的上游知识。需要完整
参数、版本差异或边缘行为时，应回到上游固定 revision 核对。

Owner 为 Observability Logging 与当前 Incident 服务 Owner；最后复核时间为 2026-07-21；
适用版本以 Upstream revision 为准。关联问题需在内部 Incident 或变更工单中记录本快照版本。

## 最小证据集

- loki_discarded_samples_total/bytes_total 按 reason 和 tenant
- distributor、ingester、gateway 日志与 HTTP status
- 写入速率、stream/cardinality、时间戳、chunk 和对象存储健康

证据必须来自同一 Incident 时间窗口，并与健康实例或事件前基线比较。

## 可执行查询与判据

```promql
sum by (tenant, reason) (rate(loki_discarded_samples_total[5m]))
sum by (tenant, reason) (rate(loki_discarded_bytes_total[5m]))
sum by (status_code) (rate(loki_request_duration_seconds_count{route=~".*push.*"}[5m]))
```
日志过滤字段至少包含 tenant、status、reason、stream hash 和 distributor/ingester。429 按 rate limit、validation、stream limit 分流；5xx 再查 ring、WAL、对象存储和网络。不得先提高 limits。关联工单：`KB-LOKI-INGEST`。

## 诊断工作流

1. 先按 reason 分类丢弃，不把所有 429 当成同一问题。
2. rate_limited 检查 tenant 速率、burst 和日志量来源。
3. validation 检查旧/新时间戳、标签、行大小和乱序。
4. stream limit 检查高基数标签和活跃 stream。
5. 5xx 检查 ingester、ring、WAL、对象存储和网络。

官方文档提供产品行为和排查方法，不证明当前 Incident 的根因。历史经验、单条日志和当前
健康检查不能替代同窗口证据链。本快照的判定对象是“日志写入 429、验证失败、乱序、时间戳、流数量、存储或 distributor/ingester 故障。”。
形成结论前至少完成“先按 reason 分类丢弃，不把所有 429 当成同一问题。”，并记录支持证据、反证、缺失证据和置信度；
无法区分时继续采集只读证据，不通过生产写操作试错。

## AutoOnCall 审批边界

提高 limits、修改标签、丢弃规则、WAL、ring 或存储配置需 Loki Owner 审批；优先减少无价值日志和高基数来源。

变更计划必须包含 approver、canary 范围、验证查询、观察时长和 rollback 条件。Agent 只生成
只读查询、证据摘要、候选假设和变更计划，不自动执行生产写操作。

## 恢复验证

恢复结论需要同时验证原始错误消失、用户侧或调用侧恢复、产品组件指标回到基线，并在约定
观察窗口内无复发。对本主题至少复查：loki_discarded_samples_total/bytes_total 按 reason 和 tenant；distributor、ingester、gateway 日志与 HTTP status。每项验证都要保留查询时间、
筛选条件、结果摘要和 Owner。若 canary 未优于对照组，或错误率、延迟、资源消耗继续恶化，
应按已审批计划 rollback，不能把一次成功探测当作稳定恢复。

## 引用信息

- Source URL：`https://github.com/grafana/loki/blob/925c8c7c7c6feface41c5bef12c74f05c05e8c84/docs/sources/operations/troubleshooting/troubleshoot-ingest.md`
- Upstream revision：`925c8c7c7c6feface41c5bef12c74f05c05e8c84`
- License：Grafana documentation terms; upstream repository AGPL-3.0
- Snapshot updated：2026-07-21
