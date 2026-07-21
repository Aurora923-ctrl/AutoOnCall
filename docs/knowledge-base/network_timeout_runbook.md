# 网络超时与连接失败 Runbook

## 适用范围

适用于 connect timeout、read timeout、connection reset、间歇性丢包和跨可用区延迟。Owner 为
网络平台与对应服务团队。

## 首轮证据

- 按 DNS、TCP connect、TLS handshake、first byte 和 body read 分解耗时；
- 对比客户端、服务端、负载均衡器和节点指标；
- 查询重传、丢包、连接拒绝、SNAT 端口和 conntrack 使用率；
- 记录受影响目标、端口、协议、可用区、发布版本和 Incident 时间窗口。

```text
logs: connect timeout OR read timeout OR connection reset OR no route to host
metrics: tcp_retransmits, packet_loss, connect_latency, active_connections
```

## 判断路径

DNS 解析慢时转入 DNS Runbook。TLS 握手失败时转入证书 Runbook。TCP connect 慢且服务端没有
请求记录时，检查路由、防火墙、负载均衡和端口容量。服务端已接收请求但 first byte 慢时，应
调查应用或依赖，不要误判为纯网络故障。

## 处置与审批

切换路由、修改安全组、调整连接池、超时、重试、负载均衡或跨地域流量均需人工审批。先采用
单目标、单可用区或小比例 canary，并限制重试预算，防止超时触发重试风暴。

## 回滚与恢复

若丢包、错误率、跨区流量成本或下游连接数上升，应回滚。恢复要求各阶段延迟回到基线、重传
与连接失败恢复、核心请求成功，并持续观察至少 30 分钟。
