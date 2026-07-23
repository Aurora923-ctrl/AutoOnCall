# 磁盘空间与 inode 耗尽诊断 Runbook

## 文档元数据

- 适用范围：主机、容器节点、日志盘、临时目录或数据库数据盘出现容量、inode、只读挂载或预计耗尽告警。
- Owner：运行平台；数据库数据盘同时升级 DBA
- 最后复核：2026-07-21
- 关联工单：`OPS-STORAGE-RUNBOOK`
- 自动化边界：默认只允许读操作、证据汇总、dry-run 和变更计划生成。

## 事件入口与影响确认

固定磁盘事故窗口后，分别确认容量、inode、只读挂载和预计耗尽时间，按 `instance/mountpoint/device` 对比健康节点。随后用只读 `df -hT`、`df -i`、`du -x` 和 `lsof +L1` 区分大文件、小文件风暴、已删除仍占用文件、日志、备份或数据库文件增长。禁止在未确认保留策略、备份和恢复点前删除数据。

## 首轮证据清单

- filesystem usage、inode usage、增长斜率、预计耗尽时间和只读状态
- 大目录、大文件、小文件密集目录、已删除但仍被句柄占用的文件
- Docker 容器日志、日志轮转、容器镜像、临时文件、备份、WAL/binlog 和快照增长

所有证据必须带查询时间、时间范围、数据源和筛选条件。历史工单只能支持假设，不能替代当前
Incident 的实时证据。

## 指标查询

以下查询是模板，标签值必须来自 Incident 上下文：

```promql
(node_filesystem_size_bytes - node_filesystem_avail_bytes) / node_filesystem_size_bytes
(node_filesystem_files - node_filesystem_files_free) / node_filesystem_files
predict_linear(node_filesystem_avail_bytes[6h], 24 * 3600)
```

查询结果需与事件前基线、未受影响实例和最近发布进行对照，避免只看峰值。

## 日志与事件模式

- `no space left on device、磁盘空间满、需制定清理计划、read-only file system、disk quota exceeded`
- `logrotate failure、backup failure、write error`

按 release、instance、endpoint、downstream 和 error type 聚合。保留代表性样本及总量，
不要把单条错误日志当作根因结论。

## 假设排除与决策树

1. 空间高且 inode 正常：按大目录、大文件和增长来源定位。
2. 空间未满但 inode 接近耗尽：定位会话文件、缓存、分片日志或临时小文件。
3. 已删除文件仍占空间：使用 lsof +L1 确认持有进程，重载或重启属于审批变更。
4. 数据库文件或备份增长：先确认复制、恢复点和保留策略，禁止无证据删除。

每个假设都要记录“支持证据、反证、缺失证据和置信度”。无法区分时继续采集最小成本的
只读证据，不通过高风险动作试错。

## 处置计划与审批

候选动作包括：经证据确认后的清理，包括磁盘清理、删除、截断、压缩、转移、Docker 清理、扩容、修改日志轮转或数据保留策略。执行前生成变更计划，至少包含证据链接、预期收益、
影响范围、风险、执行人、审批人、canary 比例、观察时长、验证查询和回滚步骤。任何生产写
操作必须经过人工审批；AutoOnCall 不自动执行不可逆或扩大故障面的动作。

## 回滚与恢复判定

清理、压缩、转移、扩容或调整保留策略前必须记录待处理路径、文件 Owner、备份或快照状态、最低剩余空间和恢复方法。若应用写入、备份、复制、回滚镜像、IO 延迟或挂载状态恶化，立即停止并恢复原保留策略或挂载配置。恢复需要容量和 inode 均低于告警阈值，预测耗尽时间重新大于 7 天，应用与数据库写入正常，且增长斜率在 30 分钟观察窗内稳定。验证结果关联 `OPS-STORAGE-RUNBOOK`，不能以一次 `df` 结果宣告恢复。

## 长期行动项

- 分别告警容量、inode 和预计耗尽时间
- 为大目录建立 Owner、配额和保留策略
- 定期演练备份恢复并验证日志轮转

行动项必须记录 Owner、截止日期、验收方式和关联工单；完成后回写本 Runbook 的更新时间。
