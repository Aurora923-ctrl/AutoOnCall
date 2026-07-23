"""Upgrade project runbooks and official snapshots into high-value RAG documents."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "docs" / "knowledge-base"
REVIEW_DATE = "2026-07-21"


RUNBOOKS = {
    "cpu_high_usage.md": {
        "title": "CPU 使用率过高诊断 Runbook",
        "scope": "适用于 HighCPUUsage 告警：虚拟机、容器、Kubernetes 工作负载及应用进程出现持续 CPU 压力，并伴随延迟、错误率、队列或吞吐异常。",
        "owner": "运行平台与服务 Owner",
        "signals": [
            "CPU utilization、load average、CPU throttling、run queue 和上下文切换",
            "按实例、Pod、进程、线程、endpoint、release 拆分的 P95/P99、QPS 与错误率",
            "线程栈、火焰图或 profiler 摘要，以及同窗口发布和定时任务记录",
        ],
        "queries": [
            'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="<namespace>"}[5m]))',
            'sum by (pod) (rate(container_cpu_cfs_throttled_seconds_total{namespace="<namespace>"}[5m]))',
            'histogram_quantile(0.95, sum by (le, service) (rate(http_request_duration_seconds_bucket[5m])))',
        ],
        "logs": [
            "repeated stack、busy loop、timeout、retry exhausted、lock contention",
            "GC overhead、slow query、pool waiting 或 downstream timeout",
        ],
        "branches": [
            "仅单进程或少量线程接近满核：保留线程栈与 profiling，排查死循环、锁竞争和热点函数。",
            "CPU 与流量同步增长：核对容量基线、重试预算和下游余量，避免把压力转移到数据库或缓存。",
            "CPU 周期性升高：关联定时任务、补偿任务、批处理和任务互斥。",
            "CPU 是在慢SQL、慢查询、依赖超时或队列增长之后升高：把 CPU 视为伴随现象并转入对应 Runbook。",
        ],
        "changes": "限流、扩容、回滚、终止进程、调整线程池或重启实例",
        "rollback": "5xx、P95/P99、请求丢弃、下游连接数、throttling 或可用实例数恶化；canary 未优于对照组。",
        "recovery": "CPU 与 throttling 回到服务容量基线，延迟、错误率和队列恢复，且至少稳定一个业务高峰或约定观察窗口。",
        "actions": [
            "建立 endpoint、release 和线程级 profiling 基线",
            "把 CPU、throttling、QPS、重试和依赖等待放在同一看板",
            "为定时任务增加互斥、超时、并发预算和错峰策略",
        ],
        "ticket": "OPS-CPU-RUNBOOK",
    },
    "disk_high_usage.md": {
        "title": "磁盘空间与 inode 耗尽诊断 Runbook",
        "scope": "主机、容器节点、日志盘、临时目录或数据库数据盘出现容量、inode、只读挂载或预计耗尽告警。",
        "owner": "运行平台；数据库数据盘同时升级 DBA",
        "signals": [
            "filesystem usage、inode usage、增长斜率、预计耗尽时间和只读状态",
            "大目录、大文件、小文件密集目录、已删除但仍被句柄占用的文件",
            "Docker 容器日志、日志轮转、容器镜像、临时文件、备份、WAL/binlog 和快照增长",
        ],
        "queries": [
            '(node_filesystem_size_bytes - node_filesystem_avail_bytes) / node_filesystem_size_bytes',
            '(node_filesystem_files - node_filesystem_files_free) / node_filesystem_files',
            'predict_linear(node_filesystem_avail_bytes[6h], 24 * 3600)',
        ],
        "logs": ["no space left on device、磁盘空间满、需制定清理计划、read-only file system、disk quota exceeded", "logrotate failure、backup failure、write error"],
        "branches": [
            "空间高且 inode 正常：按大目录、大文件和增长来源定位。",
            "空间未满但 inode 接近耗尽：定位会话文件、缓存、分片日志或临时小文件。",
            "已删除文件仍占空间：使用 lsof +L1 确认持有进程，重载或重启属于审批变更。",
            "数据库文件或备份增长：先确认复制、恢复点和保留策略，禁止无证据删除。",
        ],
        "changes": "经证据确认后的清理，包括磁盘清理、删除、截断、压缩、转移、Docker 清理、扩容、修改日志轮转或数据保留策略",
        "rollback": "应用写入失败、备份或复制异常、部署回滚镜像被清理、IO 延迟升高或挂载状态异常。",
        "recovery": "空间与 inode 均回到容量基线，预计耗尽时间恢复，应用和数据库写入正常，增长斜率不再异常。",
        "actions": ["分别告警容量、inode 和预计耗尽时间", "为大目录建立 Owner、配额和保留策略", "定期演练备份恢复并验证日志轮转"],
        "ticket": "OPS-STORAGE-RUNBOOK",
    },
    "memory_high_usage.md": {
        "title": "内存压力、OOM 与 GC 诊断 Runbook",
        "scope": "容器、JVM、Python/Go 服务或主机进程出现 working set 增长、OOMKilled、频繁 GC 或 swap 压力。",
        "owner": "运行平台与服务 Owner",
        "signals": [
            "RSS、working set、容器 limit、swap、OOM kill、restart count",
            "JVM heap、metaspace、GC 次数、暂停时长和 GC 后存活量",
            "内存增长斜率、对象分配、缓存体积、流量和批量请求大小",
        ],
        "queries": [
            'container_memory_working_set_bytes / container_spec_memory_limit_bytes',
            'increase(kube_pod_container_status_restarts_total[30m])',
            'rate(jvm_gc_pause_seconds_sum[5m]) / rate(jvm_gc_pause_seconds_count[5m])',
        ],
        "logs": ["OutOfMemoryError、OOMKilled、GC overhead、allocation failure", "heap dump failure、memory limit exceeded、kernel oom"],
        "branches": [
            "Full GC 后占用仍持续增长：优先怀疑泄漏，保留 heap/profile 再提出重启计划。",
            "内存随流量突增且流量下降后回收：检查批量大小、序列化、大对象和背压。",
            "缓存体积增长或 TTL 缺失：评估穿透和数据库冲击，禁止直接清空缓存。",
            "容器 limit 或 JVM 参数不匹配：基于容量和 GC 证据制定 canary 配置变更。",
        ],
        "changes": "生成 dump、重启、扩容、限流、调整 JVM 参数、容器 limit 或缓存策略",
        "rollback": "GC 暂停、错误率、延迟、OOM 循环、磁盘压力或数据库 QPS 恶化。",
        "recovery": "working set 与 heap 回到基线，无新增 OOM 或长暂停，核心接口和数据一致性验证通过。",
        "actions": ["建立 GC 后存活量和内存增长斜率告警", "对上传、批处理和缓存设置大小上限", "建立 dump 脱敏、保留和清理规范"],
        "ticket": "OPS-MEMORY-RUNBOOK",
    },
    "service_unavailable.md": {
        "title": "服务不可用与大面积 5xx 诊断 Runbook",
        "scope": "用户无法访问、核心接口不可达、健康检查失败或大量 503/5xx 的场景，包括 Redis、MQ 等依赖服务不可用。",
        "owner": "服务 Owner；Incident Commander 负责跨团队协调",
        "signals": [
            "用户侧探针、入口 5xx、健康检查、QPS、P95/P99 与可用副本数",
            "按版本、实例、可用区、入口和依赖拆分的错误与超时",
            "最近发布、配置、Secret、数据库、Redis、MQ、DNS、TLS 和负载均衡状态",
        ],
        "queries": [
            'sum by (service, status) (rate(http_requests_total{status=~"5.."}[5m]))',
            'sum by (deployment) (kube_deployment_status_replicas_available)',
            'histogram_quantile(0.99, sum by (le, service) (rate(http_request_duration_seconds_bucket[5m])))',
        ],
        "logs": ["配置错误、环境变量缺失、startup failed、readiness failed、panic、OOM、config parse error", "依赖服务 connection refused、upstream timeout、DNS failure、TLS handshake failure"],
        "branches": [
            "仅新版本失败且旧版本正常：保留启动和配置差异，生成回滚或切流计划。",
            "应用存活但依赖超时：检查重试放大、熔断和依赖容量，转入相应依赖 Runbook。",
            "所有副本异常且配置刚变更：核对配置版本、Secret 引用和语法，不自动重载。",
            "仅单可用区或入口失败：检查路由、负载均衡、DNS、证书和节点状态。",
        ],
        "changes": "回滚、重启、扩容、切流、降级、限流、维护页或配置变更",
        "rollback": "canary 5xx、延迟或健康检查继续恶化；下游容量接近上限；降级破坏数据完整性或业务约束。",
        "recovery": "用户侧探针、健康检查和核心业务端到端验证通过，SLO 恢复且依赖容量稳定。",
        "actions": ["完善发布保护和配置校验", "为关键依赖建立隔离、容量预算和降级演练", "将状态沟通和复盘模板纳入 Incident 流程"],
        "ticket": "OPS-AVAILABILITY-RUNBOOK",
    },
    "slow_response.md": {
        "title": "服务慢响应与超时诊断 Runbook",
        "scope": "P95/P99 超过 SLO、请求排队、超时、吞吐下降或用户感知明显变慢，包括外部API超时、缓存穿透和数据库连接池等待。",
        "owner": "服务 Owner；涉及数据库时升级 DBA",
        "signals": [
            "P50/P95/P99、QPS、错误率、队列长度、线程池和数据库连接池等待",
            "trace 中应用、数据库、缓存、外部 API、DNS/TLS 和序列化分段耗时",
            "数据库慢查询、SQL digest、锁等待、慢命令、最近发布、配置和流量变化",
        ],
        "queries": [
            'histogram_quantile(0.95, sum by (le, service, route) (rate(http_request_duration_seconds_bucket[5m])))',
            'sum by (service) (application_db_pool_waiting)',
            'sum by (downstream) (rate(client_request_duration_seconds_sum[5m]))',
        ],
        "logs": ["slow query、pool acquire timeout、lock wait、upstream timeout", "retry exhausted、queue full、task rejected、cache miss storm"],
        "branches": [
            "数据库慢查询、SQL digest、连接持有和 pool_waiting 同时升高：只读获取 EXPLAIN，转入慢 SQL 或锁等待 Runbook。",
            "外部 API 慢：拆分 DNS、connect、TLS、first-byte 和 read 阶段。",
            "缓存命中率下降且数据库 QPS 上升：检查批量失效、穿透、热点 key 和 TTL。",
            "线程池队列增长：获取线程栈并确认阻塞点，禁止默认扩大线程池。",
        ],
        "changes": "添加索引、改写 SQL、终止会话、调整连接池或线程池、缓存、超时、限流、降级或回滚",
        "rollback": "P95/P99、错误率、队列、数据库连接、锁等待、缓存或下游错误率继续恶化。",
        "recovery": "P95/P99 回到 SLO，吞吐无异常下降，pool_waiting、慢查询和下游超时恢复并完成端到端验证。",
        "actions": ["建立 endpoint、release 和 downstream 延迟预算", "为数据库慢查询、SQL digest、连接持有和线程阻塞建立回归", "统一重试、线程池与连接池并发预算"],
        "ticket": "OPS-LATENCY-RUNBOOK",
    },
    "network_timeout_runbook.md": {
        "title": "网络超时与连接失败诊断 Runbook",
        "scope": "connect timeout、read timeout、connection reset、间歇丢包、跨可用区延迟或连接拒绝。",
        "owner": "网络平台与服务 Owner",
        "signals": ["DNS、TCP connect、TLS handshake、first byte、body read 分段耗时", "重传、丢包、SYN backlog、conntrack、SNAT 端口和活跃连接", "客户端、负载均衡、节点和服务端同窗口指标"],
        "queries": ['rate(node_netstat_Tcp_RetransSegs[5m])', 'node_nf_conntrack_entries / node_nf_conntrack_entries_limit', 'histogram_quantile(0.95, sum by (le, target) (rate(probe_duration_seconds_bucket[5m])))'],
        "logs": ["connect timeout、read timeout、connection reset、no route to host", "connection refused、TLS handshake timeout、upstream prematurely closed"],
        "branches": ["DNS 阶段慢或失败：转入 DNS Runbook。", "TLS 阶段失败：转入证书 Runbook。", "TCP connect 慢且服务端无请求：检查路由、防火墙、LB、SNAT 和端口容量。", "服务端已收请求但 first byte 慢：调查应用或下游，不判为纯网络故障。"],
        "changes": "切换路由、修改安全组、超时、重试、负载均衡、连接池或跨地域流量",
        "rollback": "丢包、错误率、跨区成本、下游连接数或重试流量上升。",
        "recovery": "各阶段延迟回到基线，重传与连接失败恢复，核心请求在客户端和服务端均验证成功。",
        "actions": ["建立分阶段连接耗时指标", "监控 SNAT、conntrack 和端口余量", "为跨区路由和重试策略建立故障演练"],
        "ticket": "INC-NET-027",
    },
    "tls_certificate_expiry_runbook.md": {
        "title": "TLS 证书过期与握手失败诊断 Runbook",
        "scope": "证书即将过期、certificate has expired、hostname mismatch、unknown authority 或 TLS handshake failure。",
        "owner": "安全平台、证书平台与服务 Owner",
        "signals": ["SNI、域名、序列号、签发者、notBefore、notAfter、SAN 和证书链", "不同节点、入口、可用区和客户端的握手结果", "续期任务、Secret/Keystore 版本、代理绑定和系统时钟"],
        "queries": ['probe_ssl_earliest_cert_expiry - time()', 'sum by (instance) (rate(probe_failed_due_to_regex[5m]))', 'sum by (target) (rate(tls_handshake_errors_total[5m]))'],
        "logs": ["certificate has expired、x509 unknown authority、hostname mismatch", "remote error: tls、handshake failure、no suitable certificate"],
        "branches": ["新证书仅部分节点生效：检查滚动发布、缓存和代理绑定。", "仅单一客户端失败：检查信任库、SNI 和系统时间。", "hostname mismatch：核对 SAN，禁止关闭校验绕过。", "链不完整：比较入口返回链和受信根。"],
        "changes": "替换证书、修改信任库、重载代理、切换入口或调整证书自动续期",
        "rollback": "握手失败率、旧客户端失败、入口健康检查或证书链验证恶化。",
        "recovery": "证书链、SAN 和有效期正确，所有入口及关键客户端握手成功，续期告警恢复。",
        "actions": ["建立 30/14/7 天分级到期告警", "演练双证书兼容和回滚", "对证书 Secret 版本和入口绑定做持续审计"],
        "ticket": "INC-TLS-011",
    },
    "dns_resolution_failure_runbook.md": {
        "title": "DNS 解析失败诊断 Runbook",
        "scope": "NXDOMAIN、SERVFAIL、解析超时、错误地址、缓存未更新或 Kubernetes CoreDNS 异常。",
        "owner": "网络平台；集群内问题同时升级 Kubernetes 平台",
        "signals": ["查询名、记录类型、resolver、客户端命名空间、TTL 和时间窗口", "NXDOMAIN 场景下权威 DNS、递归 resolver、CoreDNS、节点和受影响 Pod 的对照结果", "CoreDNS 延迟、错误率、缓存、上游转发、Service 与 EndpointSlice"],
        "queries": ['sum by (rcode) (rate(coredns_dns_responses_total[5m]))', 'histogram_quantile(0.95, sum by (le, server) (rate(coredns_dns_request_duration_seconds_bucket[5m])))', 'sum(rate(coredns_forward_healthcheck_broken_total[5m]))'],
        "logs": ["NXDOMAIN、SERVFAIL、i/o timeout、no such host", "plugin/errors、upstream timeout、loop detected"],
        "branches": ["权威记录错误：升级域名 Owner。", "权威正常但递归错误：检查 CoreDNS 缓存、负缓存和上游转发，并记录 NXDOMAIN。", "仅 Pod 失败：检查 CoreDNS、NetworkPolicy、search domain、ndots 和节点 DNS。", "解析正确但连接失败：转入网络超时 Runbook。"],
        "changes": "修改 DNS 记录、TTL、CoreDNS 配置、Service selector 或 search domain",
        "rollback": "NXDOMAIN 增加、返回错误地址、流量偏斜或后端健康恶化。",
        "recovery": "多个 resolver 返回一致结果，TTL 生效，Pod 与外部探针均能解析并连接目标。",
        "actions": ["建立 rcode、延迟和上游健康告警", "保存关键域名权威与递归对照基线", "演练错误记录和 CoreDNS 上游故障"],
        "ticket": "INC-DNS-019",
    },
    "thread_pool_exhaustion_runbook.md": {
        "title": "线程池耗尽与请求排队诊断 Runbook",
        "scope": "active threads 达上限、queue depth 增长、task rejected、请求堆积或线程长期阻塞；处置前必须核对下游容量。",
        "owner": "服务 Owner 与运行平台",
        "signals": ["active、pool size、queue depth、rejected tasks 和任务耗时", "线程栈按锁、IO、数据库、外部 API 和 CPU 热点分类", "QPS、超时、重试、连接池等待和发布版本"],
        "queries": ['application_thread_pool_active / application_thread_pool_max', 'rate(application_thread_pool_rejected_total[5m])', 'histogram_quantile(0.95, sum by (le, pool) (rate(application_task_duration_seconds_bucket[5m])))'],
        "logs": ["RejectedExecutionException、task rejected、queue full", "pool acquire timeout、deadlock、blocked thread、upstream timeout"],
        "branches": ["线程阻塞在数据库或外部 API：优先修复依赖等待。", "CPU 满且 runnable 线程多：转入 CPU Runbook。", "线程数不高但队列增长：检查单任务耗时、串行锁和背压。", "仅某版本异常：比较线程模型和超时配置。"],
        "changes": "扩大线程池、调整队列、并发、拒绝策略、限流、超时或重启",
        "rollback": "下游连接、CPU、内存、错误率或上下文切换恶化。",
        "recovery": "队列清空、拒绝率归零、线程栈不再集中阻塞，P95/P99 回到 SLO。",
        "actions": ["为每个线程池建立并发预算", "压测验证拒绝策略和背压", "将线程池、连接池和下游容量统一建模"],
        "ticket": "INC-THREAD-008",
    },
    "message_queue_backlog_runbook.md": {
        "title": "消息队列积压诊断 Runbook",
        "scope": "Kafka、RabbitMQ 等出现 consumer lag、ready messages、oldest message age、消费失败或死信增长；重放必须具备幂等保护。",
        "owner": "消息平台与业务消费者 Owner",
        "signals": ["backlog、consumer lag、最老消息年龄、生产/消费速率和失败率", "按 topic、partition、queue、consumer group 和消息类型拆分", "消费者重启、rebalance、处理耗时、下游依赖和 poison message"],
        "queries": ['sum by (topic, consumergroup) (kafka_consumergroup_lag)', 'max by (queue) (rabbitmq_queue_messages_ready)', 'rate(consumer_processing_errors_total[5m])'],
        "logs": ["rebalance、commit failed、poison message、dead letter", "consumer timeout、database timeout、rate limited、deserialization error"],
        "branches": ["生产速率突增且消费稳定：评估临时容量和预计追平时间。", "单 partition 积压：检查 key 倾斜和热点。", "失败集中同一消息：隔离 poison message 并保留内容摘要。", "消费者受下游限制：禁止盲目扩容消费者。"],
        "changes": "扩容消费者、修改并发、跳过或重放消息、调整保留期或移动死信",
        "rollback": "重复处理、顺序破坏、下游过载、错误率或 rebalance 增加。",
        "recovery": "lag 和最老消息年龄持续下降，消费速率高于生产速率，死信稳定且业务校验通过。",
        "actions": ["监控 lag 斜率和预计追平时间", "建立 poison message 隔离与审计流程", "为重放设置速率、幂等和顺序保护"],
        "ticket": "INC-MQ-023",
    },
    "kubernetes_scheduling_failure_runbook.md": {
        "title": "Kubernetes 调度失败诊断 Runbook",
        "scope": "Pod 长时间 Pending、FailedScheduling、Insufficient cpu/memory、污点不容忍、亲和性冲突或 PVC 绑定失败。",
        "owner": "Kubernetes 平台与工作负载 Owner",
        "signals": ["scheduler 事件、requests/limits、可调度节点容量和配额", "nodeSelector、affinity、topology spread、taints/tolerations、PriorityClass", "PVC、StorageClass、节点状态和 autoscaler 决策"],
        "queries": ['sum by (namespace) (kube_pod_status_phase{phase="Pending"})', 'sum by (node) (kube_node_status_allocatable{resource=~"cpu|memory"})', 'increase(scheduler_schedule_attempts_total{result="unschedulable"}[15m])'],
        "logs": ["FailedScheduling、Insufficient cpu、untolerated taint", "did not match affinity、unbound immediate PersistentVolumeClaims"],
        "branches": ["Insufficient cpu/memory：比较 requests 与可调度容量，不只看实时使用率。", "taint 或 affinity 冲突：核对部署约束和合规边界。", "PVC 未绑定：升级存储平台并检查拓扑。", "大量低优先级 Pod 被抢占：检查 PriorityClass 和容量规划。"],
        "changes": "修改 requests、affinity、taints、配额、优先级、扩容节点或删除 Pod",
        "rollback": "Pod 调度到错误节点、跨区成本、资源争抢或稳定性恶化。",
        "recovery": "Pod 成功调度并 Ready，节点 headroom、拓扑、存储和合规约束符合预期。",
        "actions": ["为 requests 与可调度容量建立看板", "在 CI 校验 affinity、toleration 和 PVC 拓扑", "演练节点池耗尽与 autoscaler 失败"],
        "ticket": "INC-K8S-031",
    },
    "mysql_lock_wait_runbook.md": {
        "title": "MySQL 锁等待与死锁诊断 Runbook",
        "scope": "lock wait timeout、deadlock、长事务、metadata lock、连接占用或 pool waiting。",
        "owner": "DBA 与服务 Owner",
        "signals": ["等待事务、阻塞事务、SQL digest、事务开始时间和持锁对象", "lock wait、deadlock、active connections、pool waiting 和复制延迟", "发布、DDL、批处理、定时任务和事务访问顺序"],
        "queries": ['rate(mysql_global_status_innodb_row_lock_waits[5m])', 'mysql_global_status_threads_connected / mysql_global_variables_max_connections', 'rate(mysql_global_status_innodb_deadlocks[5m])'],
        "logs": ["Lock wait timeout exceeded、Deadlock found", "metadata lock、waiting for table metadata lock、transaction rollback"],
        "branches": ["单个长事务阻塞大量请求：确认业务状态、影响行和回滚成本。", "DDL 引发 metadata lock：关联发布任务并停止继续扩散。", "不同更新顺序导致死锁：统一事务访问顺序。", "pool waiting 仅是结果：必须以锁图和事务证据确认。"],
        "changes": "终止会话、回滚事务、停止批处理、修改 SQL/索引、DDL 或超时",
        "rollback": "复制延迟、回滚量、错误率、数据校验或业务一致性恶化。",
        "recovery": "阻塞链消失，lock wait、deadlock 与 pool waiting 回到基线，吞吐恢复且一致性检查通过。",
        "actions": ["监控长事务、锁等待图和 metadata lock", "在发布前评估 DDL 锁风险", "为关键事务建立固定访问顺序和超时预算"],
        "ticket": "INC-MYSQL-LOCK-017",
    },
}


def render_runbook(data: dict[str, object]) -> str:
    signals = "\n".join(f"- {item}" for item in data["signals"])
    queries = "\n".join(str(item) for item in data["queries"])
    logs = "\n".join(f"- `{item}`" for item in data["logs"])
    branches = "\n".join(f"{index}. {item}" for index, item in enumerate(data["branches"], 1))
    actions = "\n".join(f"- {item}" for item in data["actions"])
    return f"""# {data["title"]}

## 文档元数据

- 适用范围：{data["scope"]}
- Owner：{data["owner"]}
- 最后复核：{REVIEW_DATE}
- 关联工单：`{data["ticket"]}`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

先固定 Incident 时间窗口、受影响服务、版本、实例、租户、可用区和用户路径。使用用户侧探针、
入口指标和业务校验确认真实影响，不用单一资源指标直接宣布根因。记录告警开始时间、SLO
偏差、受影响请求比例和数据完整性状态。

## 首轮证据清单

{signals}

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
{queries}
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

{logs}

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

{branches}

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：{data["changes"]}。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚条件

出现以下任一情况立即停止扩大并执行已批准的回滚：{data["rollback"]}

## 恢复判定

{data["recovery"]}。恢复结论必须同时包含用户侧、服务侧和依赖侧证据，并持续观察约定窗口。

## 长期行动项

{actions}

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
"""


OFFICIAL_SUMMARIES = {
    "official_kubernetes_debug_pods.md": {
        "title": "Kubernetes 官方 Pod 调试 - RAG 操作快照",
        "scope": "Pod Pending、CrashLoopBackOff、ImagePull、容器启动失败、重启和终止原因诊断。",
        "evidence": ["kubectl describe pod 的状态、容器状态和 Events", "kubectl logs --previous 与当前容器日志", "资源 requests/limits、探针、镜像和节点状态"],
        "workflow": ["先确认 Pod phase 与每个 container state。", "Pending 优先读取 scheduler Events。", "Waiting 检查 reason、镜像、Secret、ConfigMap 和探针。", "Terminated 检查 exit code、signal、OOMKilled 和 termination message。", "仅特定节点失败时比较节点条件、运行时和挂载。"],
        "boundary": "删除 Pod、修改资源、探针、镜像、调度约束或扩容节点均为生产变更，需要内部审批。",
    },
    "official_kubernetes_debug_services.md": {
        "title": "Kubernetes 官方 Service 调试 - RAG 操作快照",
        "scope": "Service 无响应、EndpointSlice 为空、DNS 正常但连接失败、kube-proxy 或网络路径异常。",
        "evidence": ["Service selector、ports、targetPort 与 EndpointSlice", "Pod 内 DNS、ClusterIP、endpoint IP 和端口的分层探测", "NetworkPolicy、kube-proxy、CNI、节点和负载均衡事件"],
        "workflow": ["确认 Service 存在且 selector 命中 Ready Pod。", "检查 EndpointSlice 地址、端口和 ready 条件。", "从 Pod 内依次验证 DNS、ClusterIP、endpoint IP。", "仅 ClusterIP 失败时检查 kube-proxy/CNI；endpoint 也失败时检查应用监听和策略。", "External LoadBalancer 问题另查云负载均衡健康与安全规则。"],
        "boundary": "修改 selector、端口、NetworkPolicy、kube-proxy、CNI 或负载均衡配置必须经平台 Owner 审批。",
    },
    "official_kubernetes_pod_failure_reason.md": {
        "title": "Kubernetes 官方容器终止原因 - RAG 操作快照",
        "scope": "需要从 exit code、signal、reason 和 termination message 判断容器失败原因。",
        "evidence": ["containerStatuses.state.terminated 与 lastState.terminated", "exitCode、signal、reason、startedAt、finishedAt", "/dev/termination-log 或 terminationMessagePolicy 产生的消息"],
        "workflow": ["优先读取 lastState，避免重启后丢失上一次原因。", "exit 137 结合 OOMKilled 与节点内存证据判断，不只凭数字。", "非零业务退出码关联应用日志和发布版本。", "信号退出关联 kubelet、探针、驱逐和人工操作记录。", "termination message 只作摘要，仍需关联完整日志。"],
        "boundary": "修改 terminationMessagePolicy、探针、资源或重启策略属于部署变更，需要服务 Owner 审批。",
    },
    "official_prometheus_alerting_practices.md": {
        "title": "Prometheus 官方告警实践 - RAG 操作快照",
        "scope": "设计能反映用户影响、可行动且低噪声的告警规则和 Runbook 链接。",
        "evidence": ["用户可见延迟、错误率、可用性与业务损失", "告警触发频率、持续时间、误报率和处置动作", "dashboard、runbook、owner 和升级路径"],
        "workflow": ["优先对症状和用户影响告警，而非每个可能原因。", "同一故障链避免上下游重复 paging。", "低流量服务使用适合的窗口和最小样本保护。", "离线任务关注完成时限、积压和失败，而非瞬时资源。", "告警必须对应明确人工动作或自动化边界。"],
        "boundary": "告警规则发布需评审阈值、for、标签、路由、抑制和回滚；禁止以告警自动触发高风险生产写操作。",
    },
    "official_prometheus_alerting_rules.md": {
        "title": "Prometheus 官方告警规则语义 - RAG 操作快照",
        "scope": "理解 alert expression、pending/firing、for、keep_firing_for、labels 和 annotations。",
        "evidence": ["表达式在历史窗口的结果与基线", "pending、firing、resolved 状态转换", "规则评估错误、模板错误和 Alertmanager 接收情况"],
        "workflow": ["先在查询界面验证 expr 和标签基数。", "使用 for 过滤短暂抖动，并验证不会掩盖快速故障。", "keep_firing_for 用于减少短暂恢复引发的反复通知。", "labels 用于路由和归属，annotations 提供摘要、dashboard 与 runbook。", "用 promtool 和历史回放验证规则。"],
        "boundary": "修改阈值、for、路由标签或 keep_firing_for 需告警 Owner 审批并保留回滚版本。",
    },
    "official_redis_clients.md": {
        "title": "Redis 官方客户端连接与 maxclients - RAG 操作快照",
        "scope": "连接拒绝、maxclients、客户端超时、输出缓冲、连接空闲和连接所有者诊断。",
        "evidence": ["INFO clients 的 connected_clients、blocked_clients 和 maxclients", "rejected_connections、CLIENT LIST 聚合与操作系统文件描述符上限", "应用连接池、重试、idle 连接、发布版本和错误率"],
        "workflow": ["比较 connected_clients 与有效上限，而非只看配置值。", "按 addr、name、user、lib-name、idle 和 flags 聚合连接所有者。", "rejected_connections 增长时关联应用 pool wait 与重试。", "检查输出缓冲异常客户端和 pub/sub、replica 类连接。", "确认容量压力是广泛增长还是单一 release/客户端导致。"],
        "boundary": "提高 maxclients、文件描述符、调整连接池、断开客户端或重启 Redis 都需要 Redis Owner 审批和 canary/回滚方案。",
    },
    "official_redis_latency.md": {
        "title": "Redis 官方延迟诊断 - RAG 操作快照",
        "scope": "Redis 命令、fork、持久化、系统、网络、swap 或客户端行为造成的延迟尖峰。",
        "evidence": ["应用端延迟、redis-cli --latency 与 intrinsic latency", "SLOWLOG、LATENCY LATEST/DOCTOR、commandstats", "latest_fork_usec、AOF/RDB、swap、CPU、网络 round-trip 和客户端连接模式"],
        "workflow": ["先区分客户端网络延迟与 Redis 服务端处理延迟。", "SLOWLOG 命中时检查命令复杂度、大 key 和 KEYS 类阻塞。", "延迟与 BGSAVE/BGREWRITEAOF 同步时检查 fork、内存和磁盘。", "检查 swap、透明大页和虚拟化基线，但不直接改内核。", "连接频繁建立或大量 round-trip 时优化客户端连接复用和 pipeline 方案。"],
        "boundary": "禁用 THP、调整持久化、内核、实例、慢命令、客户端 pipeline 或重启均为审批变更；先保存基线和回滚条件。",
    },
    "official_loki_troubleshoot_ingest.md": {
        "title": "Grafana Loki 官方写入故障 - RAG 操作快照",
        "scope": "日志写入 429、验证失败、乱序、时间戳、流数量、存储或 distributor/ingester 故障。",
        "evidence": ["loki_discarded_samples_total/bytes_total 按 reason 和 tenant", "distributor、ingester、gateway 日志与 HTTP status", "写入速率、stream/cardinality、时间戳、chunk 和对象存储健康"],
        "workflow": ["先按 reason 分类丢弃，不把所有 429 当成同一问题。", "rate_limited 检查 tenant 速率、burst 和日志量来源。", "validation 检查旧/新时间戳、标签、行大小和乱序。", "stream limit 检查高基数标签和活跃 stream。", "5xx 检查 ingester、ring、WAL、对象存储和网络。"],
        "boundary": "提高 limits、修改标签、丢弃规则、WAL、ring 或存储配置需 Loki Owner 审批；优先减少无价值日志和高基数来源。",
    },
    "official_loki_troubleshoot_query.md": {
        "title": "Grafana Loki 官方查询故障 - RAG 操作快照",
        "scope": "LogQL 解析、查询限制、超时、并发、存储、认证或无数据问题。",
        "evidence": ["HTTP status、错误消息、tenant、query hash、时间范围和 LogQL", "loki_request_duration_seconds、bytes processed、chunks scanned", "query-frontend、scheduler、querier、index gateway 和对象存储日志"],
        "workflow": ["400 先检查语法、时间范围、label matcher 和限制。", "429 检查并发、队列和 tenant 限制。", "504 缩短时间范围、增加精确 matcher、提前 line filter 并查看 query stats。", "无数据时区分 selector、保留期、写入故障和索引未就绪。", "5xx 检查 index/chunk 存储、querier 和网络，不通过无限重试放大负载。"],
        "boundary": "提高 query timeout、parallelism、limits 或修改存储配置必须审批；先通过查询优化和 canary 验证资源影响。",
    },
}


OFFICIAL_META = {
    "official_kubernetes_debug_pods.md": ("https://github.com/kubernetes/website/blob/c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/debug-application/debug-pods.md", "c3317651dc19ef683c5c4463bb6bf0602c0bf364", "CC BY 4.0"),
    "official_kubernetes_debug_services.md": ("https://github.com/kubernetes/website/blob/c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/debug-application/debug-service.md", "c3317651dc19ef683c5c4463bb6bf0602c0bf364", "CC BY 4.0"),
    "official_kubernetes_pod_failure_reason.md": ("https://github.com/kubernetes/website/blob/c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/debug-application/determine-reason-pod-failure.md", "c3317651dc19ef683c5c4463bb6bf0602c0bf364", "CC BY 4.0"),
    "official_prometheus_alerting_practices.md": ("https://github.com/prometheus/docs/blob/47c3b182327d2832daadb00d0beacfcd802e4458/docs/practices/alerting.md", "47c3b182327d2832daadb00d0beacfcd802e4458", "Apache-2.0"),
    "official_prometheus_alerting_rules.md": ("https://github.com/prometheus/prometheus/blob/2cf323988931bd586a2ab25160e46bcace9398ae/docs/configuration/alerting_rules.md", "2cf323988931bd586a2ab25160e46bcace9398ae", "Apache-2.0"),
    "official_redis_clients.md": ("https://github.com/redis/docs/blob/36a9e2dbb407116f2a9d46d0f600cebdf8e4be68/content/develop/reference/clients.md", "36a9e2dbb407116f2a9d46d0f600cebdf8e4be68", "CC BY-NC-SA 4.0 and upstream notices"),
    "official_redis_latency.md": ("https://github.com/redis/docs/blob/36a9e2dbb407116f2a9d46d0f600cebdf8e4be68/content/operate/oss_and_stack/management/optimization/latency.md", "36a9e2dbb407116f2a9d46d0f600cebdf8e4be68", "CC BY-NC-SA 4.0 and upstream notices"),
    "official_loki_troubleshoot_ingest.md": ("https://github.com/grafana/loki/blob/925c8c7c7c6feface41c5bef12c74f05c05e8c84/docs/sources/operations/troubleshooting/troubleshoot-ingest.md", "925c8c7c7c6feface41c5bef12c74f05c05e8c84", "Grafana documentation terms; upstream repository AGPL-3.0"),
    "official_loki_troubleshoot_query.md": ("https://github.com/grafana/loki/blob/925c8c7c7c6feface41c5bef12c74f05c05e8c84/docs/sources/shared/troubleshoot-query.md", "925c8c7c7c6feface41c5bef12c74f05c05e8c84", "Grafana documentation terms; upstream repository AGPL-3.0"),
}


def render_official(name: str, data: dict[str, object]) -> str:
    source, revision, license_name = OFFICIAL_META[name]
    evidence = "\n".join(f"- {item}" for item in data["evidence"])
    workflow = "\n".join(f"{index}. {item}" for index, item in enumerate(data["workflow"], 1))
    primary_evidence = "；".join(str(item) for item in data["evidence"][:2])
    first_decision = str(data["workflow"][0])
    return f"""<!-- AutoOnCall retrieval snapshot
Upstream: {source}
Upstream revision: {revision}
Retrieved: {REVIEW_DATE}
License: {license_name}
Transformation: retrieval-focused operational summary; upstream attribution preserved
-->

# {data["title"]}

## 适用范围

{data["scope"]}

本快照面向 AutoOnCall 事故诊断，只保留可用于分流、证据采集和风险判断的上游知识。需要完整
参数、版本差异或边缘行为时，应回到上游固定 revision 核对。

Owner 为对应产品平台 Owner 与当前 Incident 服务 Owner；最后复核时间为 {REVIEW_DATE}；
适用版本以 Upstream revision 为准。关联问题需在内部 Incident 或变更工单中记录本快照版本。

## 最小证据集

{evidence}

证据必须来自同一 Incident 时间窗口，并与健康实例或事件前基线比较。

## 诊断工作流

{workflow}

官方文档提供产品行为和排查方法，不证明当前 Incident 的根因。历史经验、单条日志和当前
健康检查不能替代同窗口证据链。本快照的判定对象是“{data["scope"]}”。
形成结论前至少完成“{first_decision}”，并记录支持证据、反证、缺失证据和置信度；
无法区分时继续采集只读证据，不通过生产写操作试错。

## AutoOnCall 审批边界

{data["boundary"]}

变更计划必须包含 approver、canary 范围、验证查询、观察时长和 rollback 条件。Agent 只生成
只读查询、证据摘要、候选假设和变更计划，不自动执行生产写操作。

## 恢复验证

恢复结论需要同时验证原始错误消失、用户侧或调用侧恢复、产品组件指标回到基线，并在约定
观察窗口内无复发。对本主题至少复查：{primary_evidence}。每项验证都要保留查询时间、
筛选条件、结果摘要和 Owner。若 canary 未优于对照组，或错误率、延迟、资源消耗继续恶化，
应按已审批计划 rollback，不能把一次成功探测当作稳定恢复。

## 引用信息

- Source URL：`{source}`
- Upstream revision：`{revision}`
- License：{license_name}
- Snapshot updated：{REVIEW_DATE}
"""


def main() -> None:
    for name, data in RUNBOOKS.items():
        (DOCS_DIR / name).write_text(render_runbook(data), encoding="utf-8")
        print(f"Upgraded {name}")
    for name, data in OFFICIAL_SUMMARIES.items():
        (DOCS_DIR / name).write_text(render_official(name, data), encoding="utf-8")
        print(f"Summarized {name}")


if __name__ == "__main__":
    main()
