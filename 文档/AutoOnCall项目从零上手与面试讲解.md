# AutoOnCall 项目从零上手与面试讲解

这份文档用于快速理解 AutoOnCall 的当前代码和面试表达。根目录 `README.md` 是项目主说明，完整项目分析见 `文档/AutoOnCall项目分析书.md`；这里重点讲“怎么读代码、怎么演示、怎么回答追问、哪些边界不能夸大”。

## 1. 一句话定位

AutoOnCall 是一个面向 OnCall 故障诊断场景的 RAG + AIOps Agent 原型。它把 Alertmanager 告警、结构化 Incident、RAG Runbook、Plan-Execute-Replan、工具取证、证据分析、风险审批、Trace、诊断报告和安全变更记录串成一条可解释、可审计、可评测的诊断闭环。

面试 30 秒版：

> 我做的是一个面向线上故障诊断的 AIOps Incident Agent。外部告警可以通过 Alertmanager webhook 进入系统，系统会标准化告警并创建 Incident；诊断时用 LangGraph 做 Planner、Executor、Replanner，Executor 通过 Tool Registry 调用指标、日志、K8s、Redis、MySQL、Runbook 等工具，把结果归一成 Evidence 和 ToolCallRecord。高风险动作不会自动执行，而是进入审批或 dry-run；全过程会写 Trace、报告和运行态存储，并通过前端工作台和离线评测展示。

## 2. 当前真实能力边界

| 能力 | 当前状态 | 不要夸大的点 |
| --- | --- | --- |
| RAG 问答 | 支持上传、切分、Embedding、Milvus、本地词法索引、hybrid search、rerank、引用和拒答 | 不要说是企业级多租户知识库 |
| 告警接入 | 支持 `POST /api/alerts/alertmanager`、fingerprint 去重、AlertEvent 入库、IncidentState 更新 | 目前不是完整告警压缩/抑制/排班系统 |
| AIOps 诊断 | `/api/aiops` 通过 SSE 跑 Plan-Execute-Replan | 不要说是多 Agent 自主协作系统 |
| 工具执行 | Tool Registry 统一工具契约和数据源标识 | 不要说每个工具都接了真实生产系统 |
| 证据分析 | Evidence Analyzer 识别根因、缺失证据、冲突和置信度 | 不要说是完整因果推理平台 |
| 风险控制 | 只读动作自动执行，中高风险审批，危险动作禁止 | 不要说会自动修复生产故障 |
| 审批和变更 | 支持 approve/reject、诊断恢复、pre-check、dry-run、sandbox、manual record | 审批后也不会自动执行生产写操作 |
| 存储 | SQLite/MySQL 保存告警、Trace、审批、报告、会话快照、Incident 状态和变更执行 | SQLite 不等于生产级高可用存储 |
| 前端 | 静态工作台展示 RAG、告警、诊断、事件、Trace、报告、审批、变更和评测 | 不是复杂前端工程项目 |
| 评测 | AIOps、RAG、安全变更都有离线 case | 不要把离线通过率说成线上准确率 |

## 3. 代码地图

```text
app/main.py                       FastAPI 应用入口，注册所有路由并挂载 static/
app/api/
  alerts.py                       Alertmanager webhook、告警列表和详情
  aiops.py                        AIOps 诊断 SSE、demo、run history、诊断恢复和变更入口
  chat.py                         RAG 普通和流式对话
  file.py                         文件上传和目录索引
  incidents.py                    Incident 列表、详情、Trace、Report 聚合
  approvals.py                    待审批列表和 approve/reject
  evaluations.py                  评测摘要和适配器验收摘要
  health.py                       live / ready 健康检查
app/agent/aiops/
  planner.py                      结构化计划生成和 fallback
  executor.py                     风险控制、工具执行、Evidence / ToolCallRecord 生成
  replanner.py                    继续、补查、审批、报告或升级人工
  evidence_analyzer.py            根因、缺失证据、冲突、置信度
  risk_controller.py              allow / approval_required / forbidden
app/services/
  alert_ingestion_service.py      告警标准化、去重、IncidentState 更新
  aiops_service.py                构建 LangGraph 并输出 SSE
  rag_agent_service.py            RAG 回答、引用兜底、拒答
  rag_retrieval_service.py        hybrid search、rerank、metadata filter
  sqlite_store.py / mysql_store.py 运行态存储
  trace_service.py                Trace 写入和查询
  approval_service.py             审批持久化和报告状态同步
  change_execution_service.py     pre-check、dry-run、manual record、observation
  report_generator.py             结构化诊断报告和 Markdown
app/tools/
  registry.py                     Tool Registry 和 ToolContract
  *_tool.py                       指标、日志、Trace、Redis、Runbook、上下文、消息队列等工具
app/integrations/                 Alertmanager、Prometheus、Loki、K8s、Redis、MySQL、CMDB、Ticket 等适配器
static/                           前端工作台
aiops-docs/                       RAG Runbook
eval/                             离线评测 case
tests/                            pytest 测试
```

## 4. 主链路怎么讲

### 4.1 告警到 Incident

```text
Alertmanager webhook
  -> app/api/alerts.py
  -> AlertIngestionService
  -> AlertEvent 标准化
  -> fingerprint 去重
  -> IncidentState 创建/更新
  -> /api/alerts 与 /api/incidents 可查
```

可以强调：

- 重复 firing webhook 不会重复建 Incident。
- resolved webhook 会更新告警状态。
- 已进入审批或变更阶段的 Incident 不会被 resolved webhook 覆盖。
- 默认只保存精简 payload，降低敏感数据和存储膨胀风险。

### 4.2 RAG 问答

```text
文档上传
  -> 安全文件名、扩展名、大小校验
  -> Markdown 切分
  -> DashScope Embedding
  -> Milvus + 本地词法索引

用户提问
  -> 向量召回 + 词法召回
  -> 候选融合和 rerank
  -> L2 距离阈值过滤
  -> 无可信来源拒答 / 有可信来源 grounded answer
  -> citations 兜底
```

可以强调：

- top k 不等于可信，所以有距离阈值。
- 没有可信来源时不调用模型硬答。
- 答案最终带 `source_file` 和 `chunk_id`。

### 4.3 AIOps 诊断

```text
Incident
  -> Planner 生成 PlanStep
  -> Executor 调 Tool Registry
  -> ToolExecutionResult
  -> Evidence + ToolCallRecord
  -> Evidence Analyzer
  -> Replanner
  -> Approval / Report / Escalation
```

可以强调：

- Agent 不是一次性让模型写报告，而是分步取证。
- 工具结果会归一成 Evidence，报告基于结构化证据生成。
- LLM 可以参与计划和表达，但不能绕过风险控制。

### 4.4 审批和安全变更

```text
RiskAssessment approval_required
  -> ApprovalRequest
  -> approve / reject
  -> diagnosis resume 或 safe change resume
  -> pre-check
  -> dry-run
  -> manual_record / sandbox
  -> observation / rollback recommendation
```

可以强调：

- 审批通过不是直接执行生产动作。
- 系统更像“诊断和辅助决策平台”，不是自动修复平台。
- 所有审批和变更状态都会进入 Trace / Report / IncidentState。

## 5. 推荐演示路径

1. 启动服务并打开 `http://localhost:9900`。
2. 确认 `aiops-docs/` 已上传或运行 `make upload`。
3. 在 RAG 问答里问一个 Runbook 问题，展示引用来源和拒答边界。
4. 在告警接入或 demo 场景里创建一个故障事件。
5. 发起 AIOps 诊断，观察 SSE 事件、计划、工具调用和证据。
6. 打开 Incident 详情，展示 Trace、报告、审批状态和变更记录。
7. 如果出现待审批动作，演示 approve/reject 和安全变更 dry-run。
8. 打开评测面板，说明指标来自离线回归，不能代表线上准确率。

本地命令：

```bash
make bootstrap
make up
make dev
make upload
```

完整沙箱：

```bash
make sandbox-up
powershell -ExecutionPolicy Bypass -File deploy\full-stack\seed-demo-data.ps1
make sandbox-verify
make sandbox-demo
```

验证：

```bash
make lint
make test-quick
make eval
make eval-rag
make eval-change
```

## 6. 面试亮点

亮点 1：从告警入口开始，而不是只靠手动输入。

> 外部 Alertmanager webhook 进来后会被标准化成 AlertEvent，通过 fingerprint 去重，并映射到 IncidentState，后续可以进入诊断流。

亮点 2：Plan-Execute-Replan 适合排障。

> 故障诊断是动态取证过程，Planner 先拆步骤，Executor 分步调用工具，Replanner 根据证据决定继续、补查、审批或报告。

亮点 3：工具结果结构化。

> 指标、日志、Redis、K8s、MySQL、Runbook 等结果都被包装成 ToolExecutionResult，再转成 Evidence 和 ToolCallRecord，前端和报告复用同一套证据结构。

亮点 4：RAG 有可信边界。

> 检索后按 L2 距离阈值过滤；没有可信片段就拒答；有可信片段才让模型回答，并做引用兜底。

亮点 5：风险动作不会自动执行。

> Risk Controller 在工具执行前判断 allow、approval_required 或 forbidden。审批后也只进入 dry-run、sandbox 或人工记录。

亮点 6：可观测和可评测。

> Trace、Approval、Report、IncidentState、ChangeExecution 和 AlertEvent 都能落库；离线 eval 覆盖工具命中、根因识别、审批触发、危险动作拦截和 RAG 拒答。

## 7. 常见追问

### Q1：为什么不用纯 RAG？

纯 RAG 适合回答 Runbook 问题，但不能主动查询当前指标、日志、Redis、MySQL、K8s 状态。AutoOnCall 把 RAG 作为知识依据，把实时诊断交给 Plan-Execute-Replan 和工具证据。

### Q2：怎么避免模型幻觉？

RAG 无可信来源时拒答；Planner 使用结构化输出和 fallback；Executor 的证据来自工具结果；报告生成基于 Evidence、ToolCallRecord 和 RiskAssessment，而不是完全交给模型自由生成。

### Q3：真实系统没配置怎么办？

本地演示可以使用 mock/fallback。严格环境设置 `AIOPS_MOCK_FALLBACK_ENABLED=false` 后，未配置工具会返回结构化 `not_configured` 或 `failed`，不会伪装成真实证据。

### Q4：审批通过后会自动执行吗？

不会。审批通过后可以恢复诊断闭环，也可以进入安全变更流程，但安全变更只支持 pre-check、dry-run、sandbox 或人工执行记录，不自动修改生产环境。

### Q5：为什么用 SQLite？

SQLite 适合本地演示和单副本运行，能持久化 Trace、审批、报告、告警、会话快照和事件状态。项目也支持 `AIOPS_STORAGE_BACKEND=mysql`，多副本或生产化应切 MySQL。

### Q6：评测通过率可以写成线上准确率吗？

不可以。离线评测只能说明当前确定性 case 的回归结果。简历和面试里应说“建立了离线评测集，当前以最新 `logs/eval_summary.*` 为准”，不要写成线上准确率。

### Q7：项目还差什么生产化能力？

主要是 SSO/OIDC、后台任务队列、数据库 migration、多副本部署治理、组织级审批策略、审计防篡改、线上反馈闭环和更大规模真实样本治理。这些是生产化边界，不影响校招主项目成立。

## 8. 简历写法

稳妥版本：

```text
基于 FastAPI、LangGraph、Milvus 和 DashScope 构建面向 OnCall 故障诊断的 RAG + AIOps Agent 工作台，支持 Alertmanager 告警接入、知识库问答、结构化 Incident 诊断、工具取证、人工审批、Trace 追踪、诊断报告和安全变更 dry-run 闭环。

设计 Plan-Execute-Replan 诊断流程，将故障排查拆分为 Planner、Executor、Evidence Analyzer、Replanner 和 Report Generator；通过 Tool Registry 统一指标、日志、Redis、Kubernetes、MySQL、Runbook、历史工单等工具调用，并将工具结果归一为 Evidence 和 ToolCallRecord。

设计 Human-in-the-loop 风险控制链路，对只读排查自动放行，对重启、扩缩容、回滚、配置变更等中高风险动作生成审批请求，对删除 Pod、未审核 SQL、危险 shell 等动作直接阻断，避免 Agent 自动执行生产变更。

将 AlertEvent、TraceEvent、ApprovalRequest、DiagnosisReport、IncidentState、SessionSnapshot 和 ChangeExecution 持久化到 SQLite/MySQL，支持按 incident 查询完整诊断链路、工具调用、风险决策、审批状态和 Markdown 报告。
```

短版本：

```text
实现一个面向线上故障诊断的 RAG + LangGraph Agent 系统，将 Alertmanager 告警和 Incident 输入拆解为 PlanStep，调用指标、日志、Redis、K8s、MySQL 和 Runbook 工具采集 Evidence，并通过 Replanner 动态补证据、审批或生成 DiagnosisReport；引入 Risk Controller、Approval、Trace 和离线 eval，使诊断过程可解释、可审计、可复现。
```

## 9. 学习顺序

第一遍读主链路：

1. `app/main.py`
2. `app/api/alerts.py`
3. `app/services/alert_ingestion_service.py`
4. `app/api/aiops.py`
5. `app/services/aiops_service.py`
6. `app/agent/aiops/planner.py`
7. `app/agent/aiops/executor.py`
8. `app/agent/aiops/evidence_analyzer.py`
9. `app/agent/aiops/replanner.py`
10. `app/services/report_generator.py`

第二遍读边界：

1. `app/core/auth.py`
2. `app/agent/aiops/risk_controller.py`
3. `app/services/change_execution_service.py`
4. `app/services/sqlite_store.py`
5. `app/services/mysql_store.py`
6. `tests/test_alert_ingestion_service.py`
7. `tests/test_alerts_api.py`
8. `tests/test_risk_controller.py`
9. `tests/test_change_execution_service.py`
10. `tests/test_api_contracts.py`

第三遍跑演示：

1. 上传或索引 `aiops-docs`。
2. 问一个 RAG 问题，看引用。
3. 接入或模拟一个告警。
4. 跑一次 AIOps 诊断。
5. 看 Incident、Trace、Report、Approval 和 ChangeExecution。
6. 跑离线评测并说明边界。

## 10. 最后背熟三条主线

RAG 主线：

> 上传文档 -> 切分 -> Embedding -> Milvus + 词法索引 -> 混合检索 -> 阈值过滤 -> grounded answer / 拒答 -> 引用来源。

AIOps 主线：

> Alert / Incident -> Planner -> PlanStep -> Executor -> ToolExecutionResult -> Evidence + ToolCallRecord -> Evidence Analyzer -> Replanner -> Approval / Report。

工程闭环主线：

> SSE 实时展示 -> Trace 可回放 -> SQLite/MySQL 持久化 -> Incident API 聚合 -> 前端工作台展示 -> pytest/eval 验收。
