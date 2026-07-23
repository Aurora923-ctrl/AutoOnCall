<!-- AutoOnCall retrieval snapshot
Upstream: https://github.com/prometheus/docs/blob/47c3b182327d2832daadb00d0beacfcd802e4458/docs/practices/alerting.md
Upstream revision: 47c3b182327d2832daadb00d0beacfcd802e4458
Retrieved: 2026-07-21
License: Apache-2.0
Transformation: retrieval-focused operational summary; upstream attribution preserved
-->

# Prometheus 官方告警实践 - RAG 操作快照

## 适用范围

设计能反映用户影响、可行动且低噪声的告警规则和 Runbook 链接。

本快照面向 AutoOnCall 事故诊断，只保留可用于分流、证据采集和风险判断的上游知识。需要完整
参数、版本差异或边缘行为时，应回到上游固定 revision 核对。

Owner 为 SRE Observability 与当前 Incident 服务 Owner；最后复核时间为 2026-07-21；
适用版本以 Upstream revision 为准。关联问题需在内部 Incident 或变更工单中记录本快照版本。

## 最小证据集

- 用户可见延迟、错误率、可用性与业务损失
- 告警触发频率、持续时间、误报率和处置动作
- dashboard、runbook、owner 和升级路径

证据必须来自同一 Incident 时间窗口，并与健康实例或事件前基线比较。

## 可执行查询与判据

```promql
sum(rate(http_requests_total{status=~"5.."}[5m]))
/ clamp_min(sum(rate(http_requests_total[5m])), 1)
```
候选告警必须在历史窗口回放，记录样本数、阈值、`for`、预期 page 次数和对应人工动作。低流量服务使用最小样本保护；同一故障链只保留最接近用户影响的 paging 告警，原因指标进入 dashboard。Owner：SRE Observability；关联工单：`KB-PROM-ALERT-PRACTICE`。

## 诊断工作流

1. 优先对症状和用户影响告警，而非每个可能原因。
2. 同一故障链避免上下游重复 paging。
3. 低流量服务使用适合的窗口和最小样本保护。
4. 离线任务关注完成时限、积压和失败，而非瞬时资源。
5. 告警必须对应明确人工动作或自动化边界。

官方文档提供产品行为和排查方法，不证明当前 Incident 的根因。历史经验、单条日志和当前
健康检查不能替代同窗口证据链。本快照的判定对象是“设计能反映用户影响、可行动且低噪声的告警规则和 Runbook 链接。”。
形成结论前至少完成“优先对症状和用户影响告警，而非每个可能原因。”，并记录支持证据、反证、缺失证据和置信度；
无法区分时继续采集只读证据，不通过生产写操作试错。

## AutoOnCall 审批边界

告警规则发布需评审阈值、for、标签、路由、抑制和回滚；禁止以告警自动触发高风险生产写操作。

变更计划必须包含 approver、canary 范围、验证查询、观察时长和 rollback 条件。Agent 只生成
只读查询、证据摘要、候选假设和变更计划，不自动执行生产写操作。

## 恢复验证

恢复结论需要同时验证原始错误消失、用户侧或调用侧恢复、产品组件指标回到基线，并在约定
观察窗口内无复发。对本主题至少复查：用户可见延迟、错误率、可用性与业务损失；告警触发频率、持续时间、误报率和处置动作。每项验证都要保留查询时间、
筛选条件、结果摘要和 Owner。若 canary 未优于对照组，或错误率、延迟、资源消耗继续恶化，
应按已审批计划 rollback，不能把一次成功探测当作稳定恢复。

## 引用信息

- Source URL：`https://github.com/prometheus/docs/blob/47c3b182327d2832daadb00d0beacfcd802e4458/docs/practices/alerting.md`
- Upstream revision：`47c3b182327d2832daadb00d0beacfcd802e4458`
- License：Apache-2.0
- Snapshot updated：2026-07-21
