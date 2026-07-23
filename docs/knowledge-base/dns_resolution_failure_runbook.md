# DNS 解析失败诊断 Runbook

## 文档元数据

- 适用范围：NXDOMAIN、SERVFAIL、解析超时、错误地址、缓存未更新或 Kubernetes CoreDNS 异常。
- Owner：网络平台；集群内问题同时升级 Kubernetes 平台
- 最后复核：2026-07-21
- 关联工单：`INC-DNS-019`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

固定 DNS 事故窗口后，从受影响 Pod 和健康 Pod 对同一 FQDN、记录类型和 resolver 执行 `dig +time=2 +tries=1` 对照，记录 RCODE、answer、authority、TTL 和耗时。按客户端缓存、CoreDNS、上游递归和权威 DNS 分层定位；NXDOMAIN、SERVFAIL 与 timeout 必须走不同分支。若 DNS 已返回正确地址但 TCP 仍失败，应转入网络或 Service Runbook。

## 首轮证据清单

- 查询名、记录类型、resolver、客户端命名空间、TTL 和时间窗口
- NXDOMAIN 场景下权威 DNS、递归 resolver、CoreDNS、节点和受影响 Pod 的对照结果
- CoreDNS 延迟、错误率、缓存、上游转发、Service 与 EndpointSlice

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
sum by (rcode) (rate(coredns_dns_responses_total[5m]))
histogram_quantile(0.95, sum by (le, server) (rate(coredns_dns_request_duration_seconds_bucket[5m])))
sum(rate(coredns_forward_healthcheck_broken_total[5m]))
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

- `NXDOMAIN、SERVFAIL、i/o timeout、no such host`
- `plugin/errors、upstream timeout、loop detected`

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

1. 权威记录错误：升级域名 Owner。
2. 权威正常但递归错误：检查 CoreDNS 缓存、负缓存和上游转发，并记录 NXDOMAIN。
3. 仅 Pod 失败：检查 CoreDNS、NetworkPolicy、search domain、ndots 和节点 DNS。
4. 解析正确但连接失败：转入网络超时 Runbook。

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：修改 DNS 记录、TTL、CoreDNS 配置、Service selector 或 search domain。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚与恢复判定

resolver、CoreDNS、stub domain、缓存或权威记录变更必须先 canary。若 NXDOMAIN、SERVFAIL、错误地址、跨区流量倾斜或查询延迟增加，立即恢复原 ConfigMap、上游列表或记录版本。恢复需要受影响与健康 Pod 返回一致答案，CoreDNS 错误率和 P95 回到基线，负缓存 TTL 已经过期或被受控刷新，且目标服务端到端请求成功。Owner 在 `INC-DNS-019` 记录 resolver、查询时间、TTL、变更版本和至少 30 分钟观察结果。

## 长期行动项

- 建立 rcode、延迟和上游健康告警
- 保存关键域名权威与递归对照基线
- 演练错误记录和 CoreDNS 上游故障

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
