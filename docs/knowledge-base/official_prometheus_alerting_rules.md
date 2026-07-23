<!-- AutoOnCall retrieval snapshot
Upstream: https://github.com/prometheus/prometheus/blob/2cf323988931bd586a2ab25160e46bcace9398ae/docs/configuration/alerting_rules.md
Upstream revision: 2cf323988931bd586a2ab25160e46bcace9398ae
Retrieved: 2026-07-21
License: Apache-2.0
Transformation: retrieval-focused operational summary; upstream attribution preserved
-->

# Prometheus 官方告警规则语义 - RAG 操作快照

## 适用范围

理解 alert expression、pending/firing、for、keep_firing_for、labels 和 annotations。

本快照面向 AutoOnCall 事故诊断，只保留可用于分流、证据采集和风险判断的上游知识。需要完整
参数、版本差异或边缘行为时，应回到上游固定 revision 核对。

Owner 为 SRE Observability 与当前 Incident 服务 Owner；最后复核时间为 2026-07-21；
适用版本以 Upstream revision 为准。关联问题需在内部 Incident 或变更工单中记录本快照版本。

## 最小证据集

- 表达式在历史窗口的结果与基线
- pending、firing、resolved 状态转换
- 规则评估错误、模板错误和 Alertmanager 接收情况

证据必须来自同一 Incident 时间窗口，并与健康实例或事件前基线比较。

## 可执行查询与判据

```yaml
- alert: ServiceHighErrorRate
  expr: service:http_5xx_ratio:rate5m > 0.02
  for: 10m
  labels: {severity: page, owner: payments}
  annotations: {runbook_url: "<internal-runbook>"}
```
```bash
promtool check rules <rules-file>
promtool test rules <test-file>
```
判据：语法、表达式结果、标签路由和 Alertmanager 接收必须分别验证；规则发布保留旧版本、canary group 与回滚 commit。关联工单：`KB-PROM-RULES`。

## 诊断工作流

1. 先在查询界面验证 expr 和标签基数。
2. 使用 for 过滤短暂抖动，并验证不会掩盖快速故障。
3. keep_firing_for 用于减少短暂恢复引发的反复通知。
4. labels 用于路由和归属，annotations 提供摘要、dashboard 与 runbook。
5. 用 promtool 和历史回放验证规则。

官方文档提供产品行为和排查方法，不证明当前 Incident 的根因。历史经验、单条日志和当前
健康检查不能替代同窗口证据链。本快照的判定对象是“理解 alert expression、pending/firing、for、keep_firing_for、labels 和 annotations。”。
形成结论前至少完成“先在查询界面验证 expr 和标签基数。”，并记录支持证据、反证、缺失证据和置信度；
无法区分时继续采集只读证据，不通过生产写操作试错。

## AutoOnCall 审批边界

修改阈值、for、路由标签或 keep_firing_for 需告警 Owner 审批并保留回滚版本。

变更计划必须包含 approver、canary 范围、验证查询、观察时长和 rollback 条件。Agent 只生成
只读查询、证据摘要、候选假设和变更计划，不自动执行生产写操作。

## 恢复验证

恢复结论需要同时验证原始错误消失、用户侧或调用侧恢复、产品组件指标回到基线，并在约定
观察窗口内无复发。对本主题至少复查：表达式在历史窗口的结果与基线；pending、firing、resolved 状态转换。每项验证都要保留查询时间、
筛选条件、结果摘要和 Owner。若 canary 未优于对照组，或错误率、延迟、资源消耗继续恶化，
应按已审批计划 rollback，不能把一次成功探测当作稳定恢复。

## 引用信息

- Source URL：`https://github.com/prometheus/prometheus/blob/2cf323988931bd586a2ab25160e46bcace9398ae/docs/configuration/alerting_rules.md`
- Upstream revision：`2cf323988931bd586a2ab25160e46bcace9398ae`
- License：Apache-2.0
- Snapshot updated：2026-07-21
