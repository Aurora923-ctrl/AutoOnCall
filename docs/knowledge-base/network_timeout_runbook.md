# 网络超时与连接失败诊断 Runbook

## 文档元数据

- 适用范围：connect timeout、read timeout、connection reset、间歇丢包、跨可用区延迟或连接拒绝。
- Owner：网络平台与服务 Owner
- 最后复核：2026-07-21
- 关联工单：`INC-NET-027`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

固定网络事故窗口后，把请求拆成 DNS、TCP connect、TLS handshake、first byte 和body read 五段，使用带超时的 `curl -w`、TCP 探针、mtr 摘要和服务端接收日志对照。connect 慢且服务端无请求更支持路由、LB、SNAT 或 conntrack；服务端已收到请求但first byte 慢，应转入应用或下游诊断，不能归类为纯网络故障。

## 首轮证据清单

- DNS、TCP connect、TLS handshake、first byte、body read 分段耗时
- 重传、丢包、SYN backlog、conntrack、SNAT 端口和活跃连接
- 客户端、负载均衡、节点和服务端同窗口指标

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
rate(node_netstat_Tcp_RetransSegs[5m])
node_nf_conntrack_entries / node_nf_conntrack_entries_limit
histogram_quantile(0.95, sum by (le, target) (rate(probe_duration_seconds_bucket[5m])))
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

- `connect timeout、read timeout、connection reset、no route to host`
- `connection refused、TLS handshake timeout、upstream prematurely closed`

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

1. DNS 阶段慢或失败：转入 DNS Runbook。
2. TLS 阶段失败：转入证书 Runbook。
3. TCP connect 慢且服务端无请求：检查路由、防火墙、LB、SNAT 和端口容量。
4. 服务端已收请求但 first byte 慢：调查应用或下游，不判为纯网络故障。

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：切换路由、修改安全组、超时、重试、负载均衡、连接池或跨地域流量。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚与恢复判定

路由、安全组、LB、SNAT、超时、重试或连接池变更必须先限定到单可用区或 10% canary。若丢包、重传、跨区成本、下游连接数、重试流量或 5xx 增加，立即恢复原路由和超时配置。恢复需要五阶段耗时回到基线，TCP 重传与连接失败恢复，客户端和服务端均完成核心请求，且至少 30 分钟无复发。Owner 在 `INC-NET-027` 记录探针源、目标、路径、时间窗和审批后的配置版本。

## 长期行动项

- 建立分阶段连接耗时指标
- 监控 SNAT、conntrack 和端口余量
- 为跨区路由和重试策略建立故障演练

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
