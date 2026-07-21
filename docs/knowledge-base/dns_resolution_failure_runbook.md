# DNS 解析失败 Runbook

## 适用范围

适用于 `NXDOMAIN`、`SERVFAIL`、DNS 解析超时、错误地址、缓存未更新和 Kubernetes CoreDNS 异常。
排查过程中应保留关键词 CoreDNS、NXDOMAIN、权威 DNS 记录正常和 Pod 内解析失败。
Owner 为网络平台；集群内问题同时升级 Kubernetes 平台。

## 首轮证据

- 记录查询名、记录类型、resolver、客户端网络命名空间和 Incident 时间窗口；
- 使用权威 DNS、递归 resolver 和受影响 Pod 分别查询；
- 检查缓存 TTL、负缓存、CoreDNS 延迟、错误率和上游转发；
- 核对 Service、EndpointSlice、搜索域和 `ndots`。

```bash
dig <name> A
dig @<resolver> <name> A +trace
kubectl exec <pod> -- nslookup <name>
```

## 判断路径

权威 DNS 记录错误时升级域名 Owner。权威 DNS 记录正常但递归错误时检查缓存和转发。仅 Pod
失败时检查 Kubernetes CoreDNS、NetworkPolicy、搜索域和节点 DNS。解析正确但连接失败时
转入网络超时 Runbook。

## 处置与审批

修改 DNS 记录、TTL、CoreDNS 配置、Service selector 或搜索域均需人工审批。先验证目标地址
健康，再小范围刷新或切换，避免把全部流量指向未验证后端。

## 回滚与恢复

出现 NXDOMAIN 增加、错误地址或流量偏斜时立即回滚记录。恢复要求多个 resolver 返回一致，
TTL 生效，Pod 与外部探针均可解析并连接目标。
