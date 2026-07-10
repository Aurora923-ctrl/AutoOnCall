# AutoOnCall 五分钟面试演示

这是校招场景下的默认演示路径。目标不是展示大量容器，而是说明 Agent 如何通过数据源边界、工具契约、证据充分性、人工审批、报告、反馈与评测形成受控诊断闭环。

## 容器边界

默认实时栈：

- MySQL
- Redis
- metrics-exporter
- Prometheus
- Loki
- loki-log-emitter

Milvus/RAG 是加分项。只有在讲解带引用的运行手册问答时，再通过 `make up && make upload` 单独启动。

默认面试流程不要恢复 Grafana、Alertmanager、Tempo、Jaeger、Redpanda 或 K8s mock。

## 固定命令顺序

```powershell
make interview-up
make sandbox-verify
.venv\Scripts\python.exe scripts\eval\eval_cases.py `
  --cases eval\cases.yaml `
  --env-file deploy\sandbox.env `
  --report-path logs\live_golden_eval_reports.db `
  --summary-json logs\live_golden_eval_summary_current.json `
  --summary-md logs\live_golden_eval_summary_current.md `
  --skip-rag
.venv\Scripts\python.exe scripts\eval\eval_rag_cases.py `
  --cases eval\rag_cases.yaml `
  --docs-dir docs\knowledge-base `
  --summary-json logs\rag_eval_summary_current.json `
  --summary-md logs\rag_eval_summary_current.md
.venv\Scripts\python.exe scripts\eval\eval_ragas_cases.py `
  --cases eval\rag_cases.yaml `
  --docs-dir docs\knowledge-base `
  --summary-json logs\ragas_eval_summary.json `
  --summary-md logs\ragas_eval_summary.md
.\.venv\Scripts\python.exe scripts\eval\verify_milvus_multisource_rag.py `
  --summary-json logs\milvus_multisource_verification.json `
  --summary-md logs\milvus_multisource_verification.md
.venv\Scripts\python.exe scripts\eval\build_interview_summary.py `
  --ragas-summary logs\ragas_eval_summary.json `
  --summary-json logs\interview_eval_summary.json `
  --summary-md logs\interview_eval_summary.md
```

随后打开：

- `logs/interview_eval_summary.md`
- 面试官追问 AIOps 细节时再打开 `logs/live_golden_eval_summary_current.md`
- 追问 RAG 检索时再打开 `logs/rag_eval_summary_current.md`
- 追问回答质量评测时打开 `logs/ragas_eval_summary.md`
- 追问 PDF、HTML、CSV、XLSX 是否写入 Milvus 时打开 `logs/milvus_multisource_verification.md`
- 从生成的报告数据库或网页工作台中展示一份 Redis 或 MySQL 报告
- 追问证据不足时的行为时，展示 `docs/interview/negative-boundary-cases.md`

## 五分钟讲解节奏

### 0:00-0:40：项目定位

AutoOnCall 不是聊天机器人套壳，而是将一次故障变成受控诊断流程：规划、工具执行、证据沉淀、重新规划、报告、审批边界和评测。

### 0:40-1:40：实时容器证明

运行 `make sandbox-verify`，说明 Redis、MySQL、Prometheus、Loki 以及工单、服务、发布上下文均返回真实适配器来源。关键字段是 `mock_fallback_detected=false`。

### 1:40-3:10：Redis/MySQL 黄金链路

使用 `--env-file deploy\sandbox.env --skip-rag` 运行评测，并说明：

- Redis/MySQL 是由实时适配器支持的黄金链路。
- K8s 是离线黄金回归用例，不是实时容器证据。
- 实时 AIOps 摘要故意跳过 RAG；统一摘要看 `logs/interview_eval_summary.md`，独立 RAG 摘要看 `logs/rag_eval_summary_current.md`。
- `runtime_vs_incident_boundary_hit=true` 证明报告区分当前运行状态与回放事故窗口证据。
- `evidence_sufficiency_hit=true` 证明完成态报告需要主故障域证据、现象证据和运行手册或工单上下文。
- `approval_boundary_hit=true` 证明诊断可只读执行，处置变更仍需审批。

### 3:10-4:20：证据讲解

Redis 场景展示 `Redis Evidence Timeline`：事故证据键、证据哈希、时间线、热点键上下文和实时状态。

MySQL 场景展示 `MySQL Evidence Chain`：慢 SQL、连接池等待和用户影响。

报告优先展示前九个章节，它们像一份真实的 OnCall 事故复盘草案。只有在被追问时再展开事故证据图、证据矩阵、工具调用表、Trace 摘要或运行手册引用。

### 4:20-4:40：RAGAS 质量快照

打开 `logs/interview_eval_summary.md` 并指向 `RAGAS Quality Snapshot`，明确说明：

- RAG 检索评测回答“是否召回了正确的可信来源”。
- RAGAS `id-smoke` 回答“固定回答质量回归是否满足上下文标识召回、引用、拒答边界和 OnCall 可操作性”。
- `id-smoke` 不需要评审模型密钥且可复现；`--metrics-profile full` 额外使用评审模型运行忠实度和回答相关性。

### 4:40-5:00：工程约束

最后强调系统边界：工具默认只读；证据记录包含事实、推断和不确定性；生产写操作绝不自动执行；安全处置需要审批、预检查、演练、回滚和观察；离线评测用于防止核心链路静默退化。

也要说明负例边界：证据不完整时报告会降级为 `incomplete`、`degraded` 或 `needs_human`，而不会假装结论确定。操作员反馈可以将问题归类为评测草案、RAG 文档缺口、工具缺口或报告模板问题。

具体负例见 `docs/interview/negative-boundary-cases.md`：缺少运行手册时进入 `needs_human`，K8s RBAC 被拒绝时进入 `degraded`。

## 结论对齐表述

被问到“报告结论如何落到证据上”时，可使用以下表述：

AutoOnCall 不声称做逐句事实核查，而是做结论级对齐：`root_cause`、`key_findings` 和 `remediation_suggestion` 必须回链到 `evidence_id` 或 RAG 引用。回链缺失时，报告会降级为 `needs_human`。

报告还会生成 `evidence_graph` 产物。它不是新的诊断引擎，而是针对同一份 Evidence、ToolCallRecord、假设排序和引用的读模型，用于让业务结论可审计：一份根因分析应同时具备实时事故证据以及知识库或历史依据。

## K8s 的诚实表述

被问到 K8s 场景时，可明确说明：

Redis 和 MySQL 是由本地 Docker 栈支持的实时适配器黄金链路。K8s CrashLoop/OOMKilled 当前是离线黄金回归用例；为了保持默认面试环境精简且可信，没有额外加入伪造的实时 K8s 容器。
