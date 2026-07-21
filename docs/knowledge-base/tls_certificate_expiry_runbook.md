# TLS 证书过期与握手失败 Runbook

## 适用范围

适用于证书即将过期、`certificate has expired`、hostname mismatch、unknown authority 和 TLS
handshake failure。Owner 为安全平台、证书平台与服务 Owner。

## 首轮证据

- 记录 SNI、域名、证书序列号、签发者、notBefore、notAfter 和证书链；
- 区分客户端时钟、服务端证书、代理证书和内部 CA；
- 检查证书自动续期任务、Secret/Keystore 版本和负载均衡器绑定；
- 对比不同节点、可用区和客户端的握手结果。

```bash
openssl s_client -connect <host>:443 -servername <host> -showcerts
openssl x509 -noout -subject -issuer -serial -dates
```

## 判断路径

证书已更新但部分节点仍失败时，检查缓存、滚动发布和代理绑定。只有单一客户端失败时，检查
信任库和系统时间。hostname mismatch 时不要通过关闭校验绕过。

## 处置与审批

替换证书、修改信任库、重载代理或切换入口需要人工审批。变更计划应包含证书指纹、目标范围、
双证书兼容期、回滚证书和验证命令。私钥不得进入日志或知识库。

## 回滚与恢复

若握手失败率、旧客户端失败或入口健康检查恶化，应恢复旧绑定。恢复要求证书链、域名和有效期
正确，所有入口与关键客户端握手成功，并验证续期告警。
