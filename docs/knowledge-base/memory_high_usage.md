# 内存使用率过高告警处理方案

## 适用范围

- 告警：`HighMemoryUsage`、`OOMKilled`、`FrequentGC`
- 适用对象：容器、JVM、Python/Go 服务和主机进程
- Owner：运行平台与对应服务团队

阈值应来自服务容量基线、容器 limit 和历史趋势。地域、集群、日志主题与实例标识从 Incident
上下文读取，不在 Runbook 中硬编码。

## 首轮证据

1. 固定 Incident 时间窗口和受影响实例。
2. 查询 RSS、working set、container limit、swap、OOM kill、重启次数和请求量。
3. JVM 服务补充 heap、metaspace、GC 次数、暂停时长和 GC 后存活量。
4. 记录内存增长斜率，区分突增、阶梯增长和持续泄漏。
5. 在资源允许时先保存 heap dump、GC 日志或 profile，再提出重启计划。

```text
metrics: memory_working_set, memory_limit, oom_kill, restart_count, gc_pause, heap_used
logs: OutOfMemoryError OR OOMKilled OR "GC overhead" OR allocation_failure
```

## 原因判别

### 内存泄漏

Full GC 后占用仍不下降，且随运行时间持续增长时，优先怀疑泄漏。保留堆转储并用 MAT、jcmd
或语言 profiler 查找 dominator、异常集合和引用链。重启只能止损，不能证明根因。

### 流量与大对象

内存随请求量同步突增、流量下降后可回收时，检查请求体、批量查询、序列化和大对象分配。
优先考虑流式处理、批次上限和背压。

### 缓存失控

缓存体积增长、命中率低或 TTL 缺失时，检查 key 数量、对象大小、淘汰策略和租户隔离。清空
缓存可能导致穿透和数据库冲击，必须先评估影响。

### JVM 或容器配置

检查 `-Xmx`、metaspace、GC 算法、容器 limit 和 JVM 容器感知配置。只增加内存可能延后 OOM
并扩大单次 GC 暂停，配置变更需要容量依据。

## 只读取证命令

```bash
jcmd <pid> GC.heap_info
jcmd <pid> GC.class_histogram
jstat -gc <pid> 1000
```

生成 heap dump 可能带来暂停和磁盘压力。执行前必须确认剩余磁盘、敏感数据处理方式和审批
要求，产物应写入受控 Artifact 目录。

## 处置计划与审批

重启实例、扩容、限流、调整 JVM 参数、修改容器 limit 或清理缓存前，先生成变更计划。计划
必须包含现场是否已保留、canary 范围、预期内存曲线、下游风险和回滚条件，并由人工审批。

优先采用单实例摘流或小比例 canary，避免同时重启所有副本。

## 回滚条件

- canary 重启或参数变更后 GC 暂停、错误率或延迟恶化；
- OOM、重启循环或磁盘压力增加；
- 缓存变更导致数据库 QPS 或连接池等待突增；
- 内存下降但核心功能或数据一致性检查失败。

## 恢复验证

- working set 与 heap 回到服务基线并保持稳定；
- 无新增 OOM、重启循环或长时间 GC；
- P95/P99、错误率和吞吐恢复；
- heap dump、日志和审批记录可追溯；
- 至少观察一个业务高峰或约定的稳定窗口。

## 长期行动

- 建立内存增长斜率和 GC 后存活量告警；
- 对上传、批处理和缓存设置大小上限；
- 在压测中覆盖长时间运行和泄漏检测；
- 为 dump 产物建立脱敏、保留和清理策略；
- 定期复核 JVM、容器 limit 与副本容量。
