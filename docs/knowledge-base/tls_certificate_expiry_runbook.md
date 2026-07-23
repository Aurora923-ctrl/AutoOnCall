# TLS 证书过期与握手失败诊断 Runbook

## 文档元数据

- 适用范围：证书即将过期、certificate has expired、hostname mismatch、unknown authority 或 TLS handshake failure。
- Owner：安全平台、证书平台与服务 Owner
- 最后复核：2026-07-21
- 关联工单：`INC-TLS-011`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

固定 TLS 事故窗口后，使用 `openssl s_client -servername <host> -connect <host>:443 -showcerts` 从多个节点和客户端采集证书链、serial、SAN、issuer、notBefore/notAfter 和 SNI 结果。仅部分入口失败更支持绑定或发布不一致；仅旧客户端失败时还要检查信任库与算法兼容。禁止关闭 hostname 或证书链校验作为临时处置。

## 首轮证据清单

- SNI、域名、序列号、签发者、notBefore、notAfter、SAN 和证书链
- 不同节点、入口、可用区和客户端的握手结果
- 续期任务、Secret/Keystore 版本、代理绑定和系统时钟

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
probe_ssl_earliest_cert_expiry - time()
sum by (instance) (rate(probe_failed_due_to_regex[5m]))
sum by (target) (rate(tls_handshake_errors_total[5m]))
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

- `certificate has expired、x509 unknown authority、hostname mismatch`
- `remote error: tls、handshake failure、no suitable certificate`

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

1. 新证书仅部分节点生效：检查滚动发布、缓存和代理绑定。
2. 仅单一客户端失败：检查信任库、SNI 和系统时间。
3. hostname mismatch：核对 SAN，禁止关闭校验绕过。
4. 链不完整：比较入口返回链和受信根。

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：替换证书、修改信任库、重载代理、切换入口或调整证书自动续期。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚与恢复判定

证书、Secret、Keystore、信任库、代理绑定或入口 reload 必须使用双证书或小流量 canary。若握手失败、旧客户端失败、健康检查或证书链验证恶化，立即恢复原证书绑定和代理配置。恢复需要所有入口返回预期 serial 和完整证书链，SAN 与 SNI 匹配，关键客户端握手成功，剩余有效期告警恢复并观察至少 30 分钟。Security Platform 在 `CR-TLS-2026-011`记录证书指纹、节点覆盖、审批人与回滚版本。

## 长期行动项

- 建立 30/14/7 天分级到期告警
- 演练双证书兼容和回滚
- 对证书 Secret 版本和入口绑定做持续审计

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
