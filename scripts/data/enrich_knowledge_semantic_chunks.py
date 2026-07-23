"""Enrich knowledge assets so each indexed chunk is useful on its own."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "docs" / "knowledge-base"


RUNBOOK_ENRICHMENTS = {
    "cpu_high_usage.md": {
        "entry": (
            "固定 CPU 事故窗口后，先按 `cluster/namespace/workload/pod/container` 定位压力范围，"
            "再把 CPU 核数、throttling、run queue、QPS、P95 和错误率对齐到同一发布版本。"
            "若只有单 Pod 或单线程异常，保留线程栈与 profiler 摘要；若所有副本随流量同比增长，"
            "优先验证容量与重试放大。当前 CPU 指标只能证明资源压力，不能单独证明代码热点。"
        ),
        "rollback": (
            "CPU 处置必须以 canary 和对照组为基准。若 canary 的 5xx、P95/P99、请求丢弃、"
            "throttling、下游连接数或可用副本数任一恶化，立即停止扩容、限流、参数调整或"
            "进程操作，并按审批记录恢复原副本数、原配置或原版本。恢复需要 CPU 与 throttling"
            "回到事件前容量区间，业务延迟和错误率恢复，线程栈不再出现同一热点，并持续观察"
            "至少 30 分钟或一个业务高峰。Owner 记录验证查询、时间窗口和 `OPS-CPU-RUNBOOK`。"
        ),
    },
    "disk_high_usage.md": {
        "entry": (
            "固定磁盘事故窗口后，分别确认容量、inode、只读挂载和预计耗尽时间，按"
            " `instance/mountpoint/device` 对比健康节点。随后用只读 `df -hT`、`df -i`、"
            "`du -x` 和 `lsof +L1` 区分大文件、小文件风暴、已删除仍占用文件、日志、备份或"
            "数据库文件增长。禁止在未确认保留策略、备份和恢复点前删除数据。"
        ),
        "rollback": (
            "清理、压缩、转移、扩容或调整保留策略前必须记录待处理路径、文件 Owner、备份或"
            "快照状态、最低剩余空间和恢复方法。若应用写入、备份、复制、回滚镜像、IO 延迟或"
            "挂载状态恶化，立即停止并恢复原保留策略或挂载配置。恢复需要容量和 inode 均低于"
            "告警阈值，预测耗尽时间重新大于 7 天，应用与数据库写入正常，且增长斜率在 30 分钟"
            "观察窗内稳定。验证结果关联 `OPS-STORAGE-RUNBOOK`，不能以一次 `df` 结果宣告恢复。"
        ),
    },
    "dns_resolution_failure_runbook.md": {
        "entry": (
            "固定 DNS 事故窗口后，从受影响 Pod 和健康 Pod 对同一 FQDN、记录类型和 resolver"
            " 执行 `dig +time=2 +tries=1` 对照，记录 RCODE、answer、authority、TTL 和耗时。"
            "按客户端缓存、CoreDNS、上游递归和权威 DNS 分层定位；NXDOMAIN、SERVFAIL 与 timeout"
            " 必须走不同分支。若 DNS 已返回正确地址但 TCP 仍失败，应转入网络或 Service Runbook。"
        ),
        "rollback": (
            "resolver、CoreDNS、stub domain、缓存或权威记录变更必须先 canary。若 NXDOMAIN、"
            "SERVFAIL、错误地址、跨区流量倾斜或查询延迟增加，立即恢复原 ConfigMap、上游列表或"
            "记录版本。恢复需要受影响与健康 Pod 返回一致答案，CoreDNS 错误率和 P95 回到基线，"
            "负缓存 TTL 已经过期或被受控刷新，且目标服务端到端请求成功。Owner 在"
            " `INC-DNS-019` 记录 resolver、查询时间、TTL、变更版本和至少 30 分钟观察结果。"
        ),
    },
    "kubernetes_scheduling_failure_runbook.md": {
        "entry": (
            "固定 Pending 窗口后，先保存 `kubectl describe pod` 的 FailedScheduling Events，"
            "再计算目标 Pod requests 与候选节点 allocatable 的差额，并逐项核对 taint/toleration、"
            "node affinity、topology spread、PVC 绑定和 PriorityClass。仅当事件和候选节点计算"
            "同时支持时，才把资源或调度约束列为主因；ImagePull 或应用 Crash 不属于调度失败。"
        ),
        "rollback": (
            "requests、affinity、taint、PDB、PriorityClass、PVC 或节点池调整必须先作用于单个"
            " canary 工作负载。若出现错误可用区放置、资源争用、驱逐、PDB 受损、成本异常或新"
            " Pending，立即恢复原 workload spec 或节点池配置。恢复需要目标副本全部 Scheduled"
            " 且 Ready，FailedScheduling 不再增长，节点余量仍满足安全预算，批处理吞吐恢复，"
            "并观察至少一个调度周期。Owner 把事件快照、容量计算和 `CR-K8S-2026-031` 写入记录。"
        ),
    },
    "memory_high_usage.md": {
        "entry": (
            "固定内存事故窗口后，区分容器 working set、RSS、JVM heap、节点可用内存与 swap，"
            "并保存 OOMKilled、kernel OOM、GC pause 和 restart 时间线。Full GC 后仍持续增长更"
            "支持泄漏；流量下降后内存回落更支持负载或批量对象。heap dump、pprof 和 core 文件"
            "可能带来 IO、容量和敏感数据风险，必须先估算大小、脱敏和保留位置。"
        ),
        "rollback": (
            "dump、重启、扩容、JVM 参数、容器 limit 或缓存策略变更必须经审批并从 canary 开始。"
            "若 GC pause、OOM 循环、磁盘占用、错误率、P95 或数据库 QPS 恶化，立即恢复原参数、"
            "limit 或版本，并停止继续采样。恢复需要 working set 和 GC 后存活量回到基线，无新增"
            " OOMKilled，核心接口和数据一致性验证通过，且 30 分钟内内存斜率不再异常。Owner"
            "记录 dump hash、存储位置、删除期限和 `OPS-MEMORY-RUNBOOK`。"
        ),
    },
    "message_queue_backlog_runbook.md": {
        "entry": (
            "固定积压窗口后，分别记录 backlog、oldest age、producer rate、consumer rate、"
            "partition/queue 分布和失败消息键。Kafka 重点查看 consumer group lag、partition"
            "热点与 rebalance；RabbitMQ 重点查看 ready/unacked、consumer utilization 和 redelivery。"
            "追平时间按 `backlog / max(consumer_rate - producer_rate, 1)` 估算，避免盲目扩容。"
        ),
        "rollback": (
            "隔离 poison message、DLQ、增加消费者、跳过或重放消息都必须记录消息范围、幂等保护、"
            "顺序要求、速率上限和审批人。若重复处理、rebalance、下游错误、数据库连接或 oldest age"
            "恶化，立即停止重放并恢复原消费并发。恢复需要净消费速率持续高于生产速率，oldest age"
            "回到 SLO，失败消息已隔离且业务幂等校验通过。Owner 将 offset/queue 范围、速率和"
            " `INC-MQ-023` 写入审计记录。"
        ),
    },
    "mysql_lock_wait_runbook.md": {
        "entry": (
            "固定锁等待窗口后，通过只读 `performance_schema.data_lock_waits`、"
            "`data_locks`、`events_transactions_current` 和 processlist 构建 blocker -> waiter"
            " 锁图，记录事务年龄、锁类型、对象、SQL digest、连接池等待与复制延迟。"
            "pool_waiting 是影响信号，不是锁根因；只有锁图、事务时间和回退后的因果反转一致时"
            "才能确认阻塞事务。"
        ),
        "rollback": (
            "通知 Owner、暂停批任务、终止会话、回滚事务或调整 DDL 都属于受控数据库变更。"
            "审批记录必须包含 blocker 身份、未提交数据范围、复制影响、业务补偿和一致性检查。"
            "若 replica lag、rollback volume、数据差异、死锁或错误率增加，立即停止进一步终止并"
            "按 DBA 计划恢复。恢复需要锁等待图清空、pool_waiting 回到零、业务超时恢复、复制稳定，"
            "并完成关键账务校验。结果关联 `CR-LOCK-2026-017`。"
        ),
    },
    "network_timeout_runbook.md": {
        "entry": (
            "固定网络事故窗口后，把请求拆成 DNS、TCP connect、TLS handshake、first byte 和"
            "body read 五段，使用带超时的 `curl -w`、TCP 探针、mtr 摘要和服务端接收日志对照。"
            "connect 慢且服务端无请求更支持路由、LB、SNAT 或 conntrack；服务端已收到请求但"
            "first byte 慢，应转入应用或下游诊断，不能归类为纯网络故障。"
        ),
        "rollback": (
            "路由、安全组、LB、SNAT、超时、重试或连接池变更必须先限定到单可用区或 10% canary。"
            "若丢包、重传、跨区成本、下游连接数、重试流量或 5xx 增加，立即恢复原路由和超时配置。"
            "恢复需要五阶段耗时回到基线，TCP 重传与连接失败恢复，客户端和服务端均完成核心请求，"
            "且至少 30 分钟无复发。Owner 在 `INC-NET-027` 记录探针源、目标、路径、时间窗和"
            "审批后的配置版本。"
        ),
    },
    "service_unavailable.md": {
        "entry": (
            "固定不可用窗口后，从用户探针、入口 5xx、健康检查、可用副本和依赖错误建立影响面，"
            "按 `region/zone/release/instance/route/downstream` 拆分。只有新版本失败而旧版本正常"
            "时优先检查发布；所有版本同时失败时优先检查共享配置、Secret、入口或关键依赖。"
            "本 Runbook 负责跨域分流，定位到 DNS、TLS、Redis、MQ 或数据库后应转入专项文档。"
        ),
        "rollback": (
            "回滚、切流、扩容、重启、降级、限流或维护页必须基于明确的故障域和审批计划。"
            "若 canary 5xx、延迟、健康检查、下游容量或数据完整性恶化，立即恢复原版本、原流量"
            "权重或原降级策略。恢复需要用户探针、入口、服务副本和关键依赖同时正常，核心业务"
            "端到端校验通过，SLO 在 30 分钟观察窗内稳定。Incident Commander 将验证结果和"
            " `OPS-AVAILABILITY-RUNBOOK` 关联，不能只以 Pod Ready 判定业务恢复。"
        ),
    },
    "slow_response.md": {
        "entry": (
            "固定延迟窗口后，用 trace 将总耗时拆成应用执行、数据库、缓存、外部 API、DNS/TLS、"
            "线程池排队和序列化，并按 route、release 与实例对比 P50/P95/P99。pool_waiting 与"
            "SQL hold time 同升时优先慢 SQL 或锁；线程队列增长且下游 span 变长时优先下游阻塞。"
            "资源指标只能解释伴随压力，不能替代分段耗时证据。"
        ),
        "rollback": (
            "索引、SQL、连接池、线程池、缓存、超时、限流、降级或版本回滚都必须先验证一个 canary。"
            "若 P95/P99、错误率、队列、锁等待、缓存穿透、下游错误或连接数恶化，立即恢复原配置"
            "或版本。恢复需要目标 route 延迟回到 SLO，pool_waiting、慢查询、线程队列和下游"
            "超时恢复，吞吐无异常下降，并完成端到端业务校验。Owner 记录 trace 样本、查询时间"
            "和 `OPS-LATENCY-RUNBOOK` 的观察结论。"
        ),
    },
    "thread_pool_exhaustion_runbook.md": {
        "entry": (
            "固定线程池事故窗口后，记录 active、pool size、queue depth、rejected tasks 和任务"
            "等待时间，并采集两次间隔线程栈区分 CPU busy、锁等待、数据库等待、外部 API 和"
            "无界队列。仅扩大线程池可能把压力转移到数据库或下游；必须先证明阻塞点和并发预算。"
            "JVM 使用 `jcmd Thread.print`，其他运行时使用等价的只读 profile，并对敏感参数脱敏。"
        ),
        "rollback": (
            "调整线程数、队列、超时、并发、隔离舱或重启实例必须从 canary 开始，并核对下游容量。"
            "若 rejected tasks、数据库连接、下游并发、CPU、错误率或队列等待恶化，立即恢复原"
            "线程池配置或版本。恢复需要队列持续下降、rejected tasks 归零、线程栈不再集中于同一"
            "阻塞点、P99 回到 SLO，且下游未出现压力转移。Owner 保存线程栈 hash、采集时间和"
            " `CR-THREAD-2026-008` 的验证记录。"
        ),
    },
    "tls_certificate_expiry_runbook.md": {
        "entry": (
            "固定 TLS 事故窗口后，使用 `openssl s_client -servername <host> -connect <host>:443"
            " -showcerts` 从多个节点和客户端采集证书链、serial、SAN、issuer、notBefore/notAfter"
            " 和 SNI 结果。仅部分入口失败更支持绑定或发布不一致；仅旧客户端失败时还要检查信任库"
            "与算法兼容。禁止关闭 hostname 或证书链校验作为临时处置。"
        ),
        "rollback": (
            "证书、Secret、Keystore、信任库、代理绑定或入口 reload 必须使用双证书或小流量 canary。"
            "若握手失败、旧客户端失败、健康检查或证书链验证恶化，立即恢复原证书绑定和代理配置。"
            "恢复需要所有入口返回预期 serial 和完整证书链，SAN 与 SNI 匹配，关键客户端握手成功，"
            "剩余有效期告警恢复并观察至少 30 分钟。Security Platform 在 `CR-TLS-2026-011`"
            "记录证书指纹、节点覆盖、审批人与回滚版本。"
        ),
    },
}


OFFICIAL_QUERIES = {
    "official_kubernetes_debug_pods.md": (
        "```bash\n"
        "kubectl get pod <pod> -n <namespace> -o wide\n"
        "kubectl describe pod <pod> -n <namespace>\n"
        "kubectl logs <pod> -n <namespace> -c <container> --previous --since=30m\n"
        "kubectl get pod <pod> -n <namespace> -o jsonpath="
        "'{range .status.containerStatuses[*]}{.name}{\"\\t\"}{.state}{\"\\t\"}"
        "{.lastState}{\"\\n\"}{end}'\n"
        "```\n"
        "判据：Pending 必须有 scheduler Event；CrashLoop 必须保留当前与 previous 日志；"
        "OOM 需要 reason、limit 和节点内存同时支持。命令均为只读，输出记录 namespace、"
        "pod UID、resourceVersion 和采集时间。关联工单：`KB-K8S-POD-DEBUG`。"
    ),
    "official_kubernetes_debug_services.md": (
        "```bash\n"
        "kubectl get svc <service> -n <namespace> -o yaml\n"
        "kubectl get endpointslice -n <namespace> -l kubernetes.io/service-name=<service> -o wide\n"
        "kubectl run netcheck --rm -i --restart=Never --image=curlimages/curl -- "
        "sh -c 'nslookup <service> && curl -sv --max-time 3 http://<service>:<port>/health'\n"
        "```\n"
        "判据：selector 必须命中 Ready Pod，EndpointSlice 的端口与 targetPort 一致；"
        "endpoint IP 成功但 ClusterIP 失败才优先 kube-proxy/CNI。临时探测 Pod 的创建需符合"
        "集群审批和清理策略。关联工单：`KB-K8S-SERVICE-DEBUG`。"
    ),
    "official_kubernetes_pod_failure_reason.md": (
        "```bash\n"
        "kubectl get pod <pod> -n <namespace> -o jsonpath="
        "'{range .status.containerStatuses[*]}{.name}{\"\\t\"}{.lastState.terminated.reason}"
        "{\"\\t\"}{.lastState.terminated.exitCode}{\"\\t\"}"
        "{.lastState.terminated.finishedAt}{\"\\n\"}{end}'\n"
        "kubectl logs <pod> -n <namespace> -c <container> --previous --since=30m\n"
        "```\n"
        "判据：exit 137 只有与 OOMKilled、limit 或节点压力一致时才支持内存假设；"
        "SIGTERM 需要关联 rollout、探针或驱逐事件。termination message 是摘要，不能替代完整日志。"
        "关联工单：`KB-K8S-TERMINATION-REASON`。"
    ),
    "official_loki_troubleshoot_ingest.md": (
        "```promql\n"
        "sum by (tenant, reason) (rate(loki_discarded_samples_total[5m]))\n"
        "sum by (tenant, reason) (rate(loki_discarded_bytes_total[5m]))\n"
        "sum by (status_code) (rate(loki_request_duration_seconds_count{route=~\".*push.*\"}[5m]))\n"
        "```\n"
        "日志过滤字段至少包含 tenant、status、reason、stream hash 和 distributor/ingester。"
        "429 按 rate limit、validation、stream limit 分流；5xx 再查 ring、WAL、对象存储和网络。"
        "不得先提高 limits。关联工单：`KB-LOKI-INGEST`。"
    ),
    "official_loki_troubleshoot_query.md": (
        "```logql\n"
        "{cluster=\"<cluster>\", namespace=\"<namespace>\"} |= \"<error>\" | json\n"
        "sum by (status_code) (rate({component=~\"query-frontend|querier\"} | json [5m]))\n"
        "```\n"
        "记录 query hash、tenant、start/end、matcher、line filter、bytes 和 chunks。400 检查语法；"
        "429 检查 tenant 并发与队列；504 先缩短时间范围并前置精确 matcher；无数据要区分"
        "selector、retention、ingest delay 和索引就绪。关联工单：`KB-LOKI-QUERY`。"
    ),
    "official_prometheus_alerting_practices.md": (
        "```promql\n"
        "sum(rate(http_requests_total{status=~\"5..\"}[5m]))\n"
        "/ clamp_min(sum(rate(http_requests_total[5m])), 1)\n"
        "```\n"
        "候选告警必须在历史窗口回放，记录样本数、阈值、`for`、预期 page 次数和对应人工动作。"
        "低流量服务使用最小样本保护；同一故障链只保留最接近用户影响的 paging 告警，原因指标"
        "进入 dashboard。Owner：SRE Observability；关联工单：`KB-PROM-ALERT-PRACTICE`。"
    ),
    "official_prometheus_alerting_rules.md": (
        "```yaml\n"
        "- alert: ServiceHighErrorRate\n"
        "  expr: service:http_5xx_ratio:rate5m > 0.02\n"
        "  for: 10m\n"
        "  labels: {severity: page, owner: payments}\n"
        "  annotations: {runbook_url: \"<internal-runbook>\"}\n"
        "```\n"
        "```bash\npromtool check rules <rules-file>\npromtool test rules <test-file>\n```\n"
        "判据：语法、表达式结果、标签路由和 Alertmanager 接收必须分别验证；规则发布保留旧版本、"
        "canary group 与回滚 commit。关联工单：`KB-PROM-RULES`。"
    ),
    "official_redis_clients.md": (
        "```text\n"
        "redis-cli -h <host> -p <port> INFO clients\n"
        "redis-cli -h <host> -p <port> INFO stats\n"
        "redis-cli -h <host> -p <port> --raw CLIENT LIST\n"
        "```\n"
        "计算 `effective_capacity=min(maxclients, os_fd_limit-reserved_fds)`，记录"
        "connected_clients、blocked_clients、rejected_connections 和按 user/lib-name/addr 聚合的"
        "连接所有者。CLIENT LIST 输出必须脱敏。若 connected_clients 接近 effective_capacity，"
        "先确认 blocked/rejected 是否同窗增长、连接是否由单一 release 或 retry path 产生，再"
        "评估限流、降低重试或容量变更。maxclients 调高、文件描述符调整、连接池重配和重启"
        "都必须有审批、10% canary、观察窗口和 rollback 条件；关联工单：`KB-REDIS-CLIENTS`。"
    ),
    "official_redis_latency.md": (
        "```text\n"
        "redis-cli -h <host> -p <port> --latency-history -i 1\n"
        "redis-cli -h <host> -p <port> SLOWLOG GET 32\n"
        "redis-cli -h <host> -p <port> LATENCY LATEST\n"
        "redis-cli -h <host> -p <port> INFO commandstats\n"
        "```\n"
        "先比较客户端 RTT 与 Redis intrinsic latency；再按慢命令、fork/AOF/RDB、swap/THP 和"
        "连接往返分流。生产 SLOWLOG 参数和 key 必须脱敏，不自动执行内核或持久化变更。"
        "关联工单：`KB-REDIS-LATENCY`。"
    ),
}


def replace_section(text: str, heading: str, body: str) -> str:
    pattern = re.compile(
        rf"(?ms)^## {re.escape(heading)}\n.*?(?=^## |\Z)",
    )
    replacement = f"## {heading}\n\n{body.strip()}\n\n"
    updated, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise ValueError(f"section not found: {heading}")
    return updated


def replace_range(text: str, start_heading: str, end_heading: str, replacement: str) -> str:
    pattern = re.compile(
        rf"(?ms)^## {re.escape(start_heading)}\n.*?(?=^## {re.escape(end_heading)}\n)",
    )
    updated, count = pattern.subn(replacement.rstrip() + "\n\n", text, count=1)
    if count != 1:
        raise ValueError(f"section range not found: {start_heading} -> {end_heading}")
    return updated


def enrich_runbooks() -> None:
    for name, enrichment in RUNBOOK_ENRICHMENTS.items():
        path = DOCS_DIR / name
        text = path.read_text(encoding="utf-8")
        text = replace_section(text, "事件入口与影响确认", enrichment["entry"])
        if "## 回滚与恢复判定" in text:
            text = replace_section(text, "回滚与恢复判定", enrichment["rollback"])
        else:
            text = replace_range(
                text,
                "回滚条件",
                "长期行动项",
                f"## 回滚与恢复判定\n\n{enrichment['rollback']}",
            )
        path.write_text(text, encoding="utf-8")


def enrich_official_snapshots() -> None:
    for name, query_body in OFFICIAL_QUERIES.items():
        path = DOCS_DIR / name
        text = path.read_text(encoding="utf-8")
        if "## 可执行查询与判据" not in text:
            text = text.replace(
                "## 诊断工作流\n",
                f"## 可执行查询与判据\n\n{query_body}\n\n## 诊断工作流\n",
                1,
            )
        text = text.replace(
            "Owner 为对应产品平台 Owner 与当前 Incident 服务 Owner；",
            f"Owner 为 {official_owner(name)} 与当前 Incident 服务 Owner；",
            1,
        )
        path.write_text(text, encoding="utf-8")


def official_owner(name: str) -> str:
    if "kubernetes" in name:
        return "Kubernetes Platform"
    if "loki" in name:
        return "Observability Logging"
    if "prometheus" in name:
        return "SRE Observability"
    return "Redis Service Team"


def enrich_html_roles() -> None:
    payment_path = DOCS_DIR / "payment_wiki.html"
    payment = payment_path.read_text(encoding="utf-8")
    payment = re.sub(
        r"(?s)<h2>Recovery criteria and history</h2>.*?</p>",
        (
            "<h2>Recovery criteria and document role</h2>\n"
            "<p>Recovery requires pool_waiting=0, active connections below the approved service "
            "capacity threshold, payment P95 below the service SLO, stable replication, and no "
            "duplicate-charge signal throughout the observation window. This wiki is the live "
            "diagnostic decision guide; the PDF is the historical postmortem and tickets.xlsx is "
            "the structured incident catalog. Do not copy historical incident values from those "
            "sources into a current diagnosis without incident-window evidence.</p>"
        ),
        payment,
        count=1,
    )
    payment_path.write_text(payment, encoding="utf-8")

    redis_path = DOCS_DIR / "redis_capacity_wiki.html"
    redis = redis_path.read_text(encoding="utf-8")
    redis = re.sub(
        r"(?s)<h2>Historical context</h2>.*?</p>",
        (
            "<h2>Document role and historical boundary</h2>\n"
            "<p>This wiki is the live Redis client-capacity decision guide. The PDF preserves a "
            "sanitized historical timeline, and tickets.xlsx stores structured incident records. "
            "Historical client counts and approvals are hypotheses only; current diagnosis must "
            "use same-window INFO clients, INFO stats, application pool metrics, deploy history, "
            "and logs. Cite the source that supplied each fact rather than repeating the same "
            "incident as multiple independent votes.</p>"
        ),
        redis,
        count=1,
    )
    redis_path.write_text(redis, encoding="utf-8")


def main() -> None:
    enrich_runbooks()
    enrich_official_snapshots()
    enrich_html_roles()


if __name__ == "__main__":
    main()
