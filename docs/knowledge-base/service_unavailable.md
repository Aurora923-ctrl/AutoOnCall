# 服务不可用与大面积 5xx 诊断 Runbook

## 文档元数据

- 适用范围：用户无法访问、核心接口不可达、健康检查失败或大量 503/5xx 的场景，包括 Redis、MQ 等依赖服务不可用。
- Owner：服务 Owner；Incident Commander 负责跨团队协调
- 最后复核：2026-07-21
- 关联工单：`OPS-AVAILABILITY-RUNBOOK`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

固定不可用窗口后，从用户探针、入口 5xx、健康检查、可用副本和依赖错误建立影响面，按 `region/zone/release/instance/route/downstream` 拆分。只有新版本失败而旧版本正常时优先检查发布；所有版本同时失败时优先检查共享配置、Secret、入口或关键依赖。本 Runbook 负责跨域分流，定位到 DNS、TLS、Redis、MQ 或数据库后应转入专项文档。

## 首轮证据清单

- 用户侧探针、入口 5xx、健康检查、QPS、P95/P99 与可用副本数
- 按版本、实例、可用区、入口和依赖服务拆分错误与超时，并核对重试放大与熔断状态
- 最近发布、配置、Secret、数据库、Redis、MQ、DNS、TLS 和负载均衡状态

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
sum by (service, status) (rate(http_requests_total{status=~"5.."}[5m]))
sum by (deployment) (kube_deployment_status_replicas_available)
histogram_quantile(0.99, sum by (le, service) (rate(http_request_duration_seconds_bucket[5m])))
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

- `配置错误、环境变量缺失、startup failed、readiness failed、panic、OOM、config parse error`
- `依赖服务 connection refused、upstream timeout、DNS failure、TLS handshake failure`

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

1. 仅新版本失败且旧版本正常：保留启动和配置差异，生成回滚或切流计划。
2. 应用存活但依赖超时：检查重试放大、熔断和依赖容量，转入相应依赖 Runbook。
3. 所有副本异常且配置刚变更：核对配置版本、Secret 引用和语法，不自动重载。
4. 仅单可用区或入口失败：检查路由、负载均衡、DNS、证书和节点状态。

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：回滚、重启、扩容、切流、降级、限流、维护页或配置变更。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚与恢复判定

回滚、切流、扩容、重启、降级、限流或维护页必须基于明确的故障域和审批计划。若 canary 5xx、延迟、健康检查、下游容量或数据完整性恶化，立即恢复原版本、原流量权重或原降级策略。恢复需要用户探针、入口、服务副本和关键依赖同时正常，核心业务端到端校验通过，SLO 在 30 分钟观察窗内稳定。Incident Commander 将验证结果和 `OPS-AVAILABILITY-RUNBOOK` 关联，不能只以 Pod Ready 判定业务恢复。

## 长期行动项

- 完善发布保护和配置校验
- 为关键依赖建立隔离、容量预算和降级演练
- 将状态沟通和复盘模板纳入 Incident 流程

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
