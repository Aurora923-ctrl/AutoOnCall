# AutoOnCall 校招项目总计划

## 1. 结论

AutoOnCall 对校招生来说已经是一个强度较高的大模型应用工程项目。它不是普通 RAG 问答，也不是套壳聊天机器人，而是围绕 OnCall 故障诊断做了告警接入、Runbook RAG、Plan-Execute-Replan Agent、工具取证、证据分析、风险审批、Trace、报告、安全变更 dry-run 和离线评测闭环。

综合判断：

> 当前项目可以作为“大模型应用开发 / AI Agent 工程 / 后端工程 / AIOps 平台”方向的校招主项目。对大模型应用开发岗位而言，没有致命核心技术栈缺口；当前综合竞争力约为 **8.95 / 10**，属于校招 A 档、接近 A+ 的项目。下一阶段更需要补的是 Redis / MySQL 两条 live golden chain 的证据矩阵、关键结论引用对齐、多源 RAG 在 RCA 主链路中的证明材料、真实边界表达和可复现面试材料，而不是继续堆新工具或新名词。

如果面试方向偏“模型算法 / 训练 / 微调”，项目会缺少模型训练、微调、推理加速和模型评测平台相关内容。但如果目标是“大模型应用开发、Agent 工程、RAG 工程、后端工程”，当前技术栈已经覆盖主干。

作为校招主打项目的多维评价：

| 维度 | 评分 | 判断 |
| --- | ---: | --- |
| 业务价值 | 8.8 | OnCall/AIOps 场景真实，告警到诊断报告闭环明确 |
| 大模型应用深度 | 8.5 | RAG、Agent、Replanner、报告生成、拒答和引用都有落地 |
| RAG 数据接入 | 8.8 | 已支持 Markdown、PDF、HTML、CSV、XLSX，并有清洗质量报告 |
| RAG 可信生成 | 8.7 | hybrid recall、rerank、trust gate、citation guard、no-answer 做得扎实 |
| Agent 设计 | 8.8 | Plan-Execute-Replan、Evidence Analyzer、Tool Registry 结构清楚 |
| 外部系统适配 | 8.8 | Redis、MySQL、Prometheus、Loki、CMDB、发布历史、工单链路可验证 |
| 安全与风控 | 9.1 | 审批、forbidden、dry-run、危险动作阻断是强亮点 |
| 证据链与复盘 | 9.0 | Evidence、ToolCallRecord、Trace、Report、Replay 形成闭环 |
| 评测体系 | 8.9 | RAG/AIOps/change/replanner + live adapter verification，校招很能打 |
| 生产化程度 | 8.0 | 有生产意识，但还缺 OIDC、多团队权限、审计防篡改等企业级能力 |
| 面试可讲性 | 9.2 | 主线完整，容易讲出工程价值 |
| 简历吸引力 | 9.1 | 明显强于普通知识库、普通 Agent、普通 LangChain Demo |

## 2. 当前项目与代码实际是否一致

本次合并前检查了当前仓库的 README、Makefile、依赖、API、Agent、工具、适配器、评测用例和文档。旧文档大方向基本贴合实际，但存在几类需要收敛的地方：

| 项目 | 当前实际 | 文档处理 |
| --- | --- | --- |
| 版本与定位 | `pyproject.toml` 与 README 均为 `1.2.1`，定位为 FastAPI + RAG + AIOps Agent | 保留 |
| 主链路 | Alert / Incident -> Planner -> Executor -> Evidence -> Replanner -> Report / Approval | 保留 |
| RAG | 支持 Markdown / PDF / HTML / CSV / XLSX 多源入库，Milvus、DashScope Embedding、本地词法索引、hybrid search、rerank、trust gate、citation guard | 更新为多源企业知识入库口径 |
| 文档 Loader 与质量报告 | `app/services/document_loaders/` 已有 plain text、PDF、HTML、table loader；`IndexingQualityService` 记录 raw/indexed/dropped/warning | 新增为校招亮点 |
| AIOps eval | `eval/cases.yaml` 当前 16 个 case | 旧文档中“25 个左右”为目标，不写成已完成 |
| RAG eval | `eval/rag_cases.yaml` 当前覆盖基础 Runbook、拒答、混淆 case，以及 PDF / HTML / 表格 loader metadata case | 更新，不写成线上准确率 |
| 安全变更 eval | `eval/change_cases.yaml` 当前 9 个 case | 保留 |
| Replanner eval | `eval/replanner_cases.yaml` 当前 4 个 case | 保留 |
| CI | `.github/workflows/quality.yml` 已存在，执行 `make verify` | 旧文档中“加一个 CI”改成“维护 CI 门禁” |
| MCP | 当前 `mcp_servers/` 主要是本地 mock/fallback，真实主链路以 Tool Registry + integrations 为准；校招主线不依赖 MCP mock | 收敛表述，不宣传为生产 MCP 接入 |
| interview/full-stack sandbox | 默认校招栈启动 MySQL、Redis、metrics-exporter、Prometheus、Loki、loki-log-emitter；CMDB、发布历史、历史工单已迁移到真实 MySQL 表；Redis 事故证据写入真实 Redis key；Milvus/RAG 通过 `make up && make upload` 作为加分项单独启动 | 更新为真实适配器口径，不再说依赖 HTTP mock、Milvus 抢主线或高级观测组件 |
| 生产化 | 有 token RBAC、生产暴露保护、SQLite/MySQL、健康检查，但不是企业级生产系统 | 保留边界 |
| 项目路径 | 旧文档出现过历史路径 | 删除 |
| 评分 | 旧文档有 86/100、87/100 两套，且未纳入 live adapter 与多源 RAG 改进 | 合并为 8.95/10 的当前校招竞争力评价 |

结论：旧文档不是方向错误，而是重复、局部过时、口径不统一。合并后只保留本文件作为校招计划和面试讲解入口。

## 3. 校招生还缺什么核心技术栈

### 3.1 已经具备的核心栈

| 技术栈 | 当前项目体现 |
| --- | --- |
| Python 后端工程 | FastAPI、Pydantic、SSE、配置管理、健康检查、API contract |
| RAG 工程 | Markdown / PDF / HTML / CSV / XLSX 多源入库、清洗报告、切分、Embedding、Milvus、词法索引、hybrid search、rerank、拒答、引用 |
| Agent 编排 | LangGraph Plan-Execute-Replan、Planner / Executor / Replanner |
| Tool Calling | Tool Registry、ToolContract、ToolExecutionResult、本地工具与 MCP 工具兼容 |
| AIOps / SRE 场景 | Alertmanager webhook、Prometheus、Loki/日志网关、K8s、Redis、MySQL、CMDB、工单、发布历史 |
| 安全边界 | Risk Controller、approval_required、forbidden、dry-run、sandbox、manual record |
| 可观测与复盘 | TraceEvent、ToolCallRecord、Evidence、DiagnosisReport、IncidentState |
| 存储 | SQLite / MySQL 运行态存储、会话快照、审批、报告、变更执行 |
| 测试与评测 | pytest、AIOps eval、RAG eval、安全变更 eval、Replanner eval、adapter verification |
| 工程门禁 | Ruff、Black、mypy、Bandit、GitHub Actions、`make verify` |
| 本地沙箱 | Docker Compose interview stack、真实 Redis/MySQL/Prometheus/Loki 适配器和固定 demo report |
| 知识治理 | loader 级 `DocumentCleaningReport`、`IndexingQualityService`、doc_type 聚合、低质量文件记录 |

这些已经足够支撑校招面试中的“我不是只会调 API，而是能把 LLM 放进工程闭环”的核心叙事。

### 3.2 不属于当前方向的“非必要缺口”

下面这些不是不好，而是不适合作为当前校招冲刺的优先项：

| 技术栈 | 是否必须 | 原因 |
| --- | --- | --- |
| 大模型预训练 / 微调 / LoRA | 不必须 | 项目定位是应用工程，不是模型算法项目 |
| 自研向量数据库 / 自研 reranker | 不必须 | 现阶段用 Milvus + 规则 rerank 更贴合工程目标 |
| 多 Agent 群聊框架 | 不必须 | 容易显得堆概念，且不如现有受控工具链可信 |
| 复杂 React / Vue 重写 | 不必须 | 前端只需支撑诊断工作台演示 |
| Helm / 多租户 / 企业级 SSO | 不必须 | 属于生产平台化，不是校招作品最优投入 |
| 自动生产修复 | 不应该做 | 风险过高，当前“诊断 + 审批 + dry-run”边界更成熟 |

### 3.3 真正建议补强的能力

对校招来说，AutoOnCall 更缺的不是“核心技术栈”，而是这些证明材料和工程边界：

| 优先级 | 需要补强 | 当前判断 | 建议目标 |
| --- | --- | --- | --- |
| P0 | 稳定面试 demo | 已有 Redis / MySQL / K8s demo 与 `make demo-reports` | 3 个 case 都有固定输入、预期工具、预期报告 |
| P0 | 证据驱动 RCA | Evidence、Report、supporting/refuting/unknown 已有基础 | 反证 case 不误判，报告明确置信度原因 |
| P0 | 评测 summary | AIOps 16、RAG 22、Change 9、Replanner 4 | AIOps 提升到 20-25 个高质量 case，增加 contradiction / tool failure |
| P0 | mock / sandbox / real 边界 | README 与工具结果已有 `source` / `source_quality` 口径 | 所有报告和面试话术都不把 mock 包装成生产数据 |
| P0 | 多源 RAG 与主链路结合 | 已支持 PDF / HTML / CSV / XLSX loader，并有 loader 清洗质量报告 | Redis / MySQL golden chain 中体现 PDF 复盘、Wiki、历史工单表格如何支撑 RCA，而不是单独炫耀“支持很多格式” |
| P1 | 证据矩阵分层 | Evidence 和 Report 已有基础字段 | 报告区分实时证据、知识依据、历史经验，根因结论至少关联两类证据 |
| P1 | 关键结论引用对齐 | citation guard 已保证有来源 | 只对 root_cause、key_findings、remediation_suggestion 做 evidence_id / citation 对齐 |
| P1 | loader 质量指标进入评测摘要 | 已有 `IndexingQualityService` 与 `/api/knowledge/indexing/reports` | eval summary 展示各 doc_type 的 indexed/dropped/warning/citation coverage |
| P2 | 后台任务化 | 当前 SSE 长诊断仍偏单请求链路 | 可考虑后台任务队列或任务状态机，但不优先于主链路材料 |
| P2 | 数据库迁移体系 | 有 SQLite/MySQL 与兼容迁移逻辑 | 企业化才需要 Alembic 等正式 migration |

## 4. 推荐项目定位

面试一句话：

> AutoOnCall 是一个面向 OnCall 故障诊断的 RAG + AIOps Agent 系统，基于 FastAPI、LangGraph、Milvus 和 Tool Registry，把告警接入、Runbook 检索、计划生成、工具取证、证据分析、风险审批、Trace、报告和安全变更 dry-run 串成可解释、可审计、可评测的诊断闭环。

30 秒版本：

> 我做的是一个面向线上故障诊断的 AIOps Incident Agent。Alertmanager webhook 或手工 Incident 进入系统后会标准化成 AlertEvent 和 IncidentState；诊断时用 LangGraph 做 Planner、Executor、Replanner，Executor 通过 Tool Registry 调用指标、日志、Redis、MySQL、K8s、Runbook、服务目录、发布历史和历史工单等工具，把结果归一成 Evidence 和 ToolCallRecord。高风险动作不会自动执行，而是进入审批或 dry-run；全过程会写 Trace、报告和运行态存储，并通过前端工作台和离线评测展示。

3 分钟版本：

1. 先把 OnCall 故障抽象成 `AlertEvent`、`IncidentState` 和 `AIOpsRequest`，系统不是只有一段用户输入。
2. 知识侧用 RAG 管理 Runbook，检索后经过 hybrid search、rerank、trust gate 和 citation guard，无可信来源时拒答。
3. 诊断侧用 LangGraph 做 Plan-Execute-Replan。Planner 生成结构化 `PlanStep`，Executor 通过 Tool Registry 调指标、日志、Redis、MySQL、K8s、Runbook、服务目录、发布历史和历史工单，Replanner 根据证据决定补查、审批或报告。
4. 工具结果统一转成 `ToolExecutionResult`、`Evidence` 和 `ToolCallRecord`，前端、报告、Trace 和评测复用同一套结构。
5. 风险控制是项目边界：只读排查自动执行，中高风险动作进入审批，危险 SQL、删 Pod、危险 shell 直接禁止；审批通过后也只进入 pre-check、dry-run、sandbox 或人工记录。
6. 最后用 pytest、AIOps eval、RAG eval、安全变更 eval、Replanner eval 和 CI 门禁做质量保障。离线评测只代表回归，不包装成线上准确率。

## 5. 主链路与代码地图

### 5.1 主业务链路

```text
Alertmanager / 手工 Incident / Demo Incident
  -> AlertEvent 或 Incident
  -> IncidentState
  -> AIOpsService
  -> Planner 生成 PlanStep
  -> Executor 调用 Tool Registry
  -> ToolExecutionResult
  -> Evidence + ToolCallRecord
  -> Evidence Analyzer
  -> Replanner 决策
  -> Approval / Report / Escalation
  -> Safe Change pre-check / dry-run / manual_record
  -> Trace + Report + Incident 工作台展示
```

### 5.2 核心目录

```text
app/main.py                         FastAPI 应用入口、路由注册、静态工作台
app/api/                            chat、file、alerts、aiops、approvals、incidents、eval、health、a2a
app/agent/aiops/                    Planner、Executor、Replanner、Evidence Analyzer、Risk Controller
app/services/                       RAG、AIOps 编排、存储、Trace、审批、报告、告警接入、读模型
app/services/aiops_read_models/     AIOps run、Incident、Replay 和评测读模型
app/models/                         Pydantic 请求、告警、证据、审批、报告、变更和 Trace 模型
app/tools/                          Tool Registry 和各类 AIOps 工具
app/integrations/                   Prometheus、Loki、K8s、Redis、MySQL、CMDB、发布历史、工单等适配器
eval/                               AIOps、RAG、安全变更、Replanner 离线评测 case
scripts/                            demo、eval、sandbox、maintenance、dev 脚本
static/                             静态诊断工作台
deploy/                             本地沙箱和生产配置说明
```

## 6. 当前能力边界

| 能力 | 当前状态 | 不要夸大的点 |
| --- | --- | --- |
| RAG 问答 | 支持上传、切分、Embedding、Milvus、本地词法索引、hybrid search、rerank、引用和拒答 | 不是企业级多租户知识库 |
| 多源知识入库 | 支持 Markdown、PDF、HTML、CSV、XLSX；PDF 保留页码，HTML 保留标题路径，表格保留 sheet、row 和 primary_key；索引后记录 cleaning report | 不宣传 OCR、Word、Confluence/Jira API 或数据库表自动同步，除非后续确实接入 |
| 告警接入 | 支持 Alertmanager webhook、fingerprint 去重、AlertEvent 入库、IncidentState 更新 | 不是完整告警压缩、排班、值班系统 |
| AIOps 诊断 | `/api/aiops` 通过 SSE 跑 Plan-Execute-Replan | 不宣传成不受控的多 Agent 自主修复系统 |
| 工具执行 | Tool Registry 统一工具契约和数据源标识 | 不说每个工具都接了真实生产系统 |
| 证据分析 | Evidence Analyzer 识别根因、缺失证据、冲突和置信度 | 不是完整因果推理平台 |
| 风险控制 | 只读动作自动执行，中高风险审批，危险动作禁止 | 不说会自动修复生产故障 |
| 审批和变更 | 支持 approve/reject、诊断恢复、pre-check、dry-run、sandbox、manual record | 审批后也不会自动改生产 |
| 存储 | SQLite/MySQL 保存告警、Trace、审批、报告、会话快照、Incident 状态和变更执行 | SQLite 不等于生产高可用存储 |
| 前端 | 静态工作台展示 RAG、告警、诊断、事件、Trace、报告、审批、变更和评测 | 不是复杂前端工程主项目 |
| MCP | 本地 CLS / Monitor MCP 偏 mock/fallback；主链路以 Tool Registry + integrations 为准 | 不宣传成生产级 MCP 工具平台 |
| 评测 | AIOps、RAG、安全变更、Replanner 都有离线 case | 不把离线通过率说成线上准确率 |

### 6.1 当前外部适配器与 mock 边界

这次外部适配器收敛后，校招主线按“真实适配器优先、mock 明确隔离”的口径讲：

| 类别 | 当前状态 | 面试口径 |
| --- | --- | --- |
| Redis / MySQL / Prometheus / Loki | Docker 中运行真实容器，工具通过真实适配器读取 `redis_info`、`mysql`、`prometheus`、`loki` 等数据源 | 这是当前主线取证链路，可以现场展示 |
| CMDB / 服务目录 | 本地不再启动 `mock-cmdb` HTTP 服务，数据写入 MySQL 表 `aiops_service_catalog`，由 `MySQLBusinessDataAdapter` 读取 | 这是业务上下文，不是 mock JSON 文件 |
| 发布历史 | 本地不再启动 `mock-deploy-history` HTTP 服务，数据写入 MySQL 表 `aiops_deploy_history` | 用来解释近期变更如何参与假设排序 |
| 历史工单 | 本地不再启动 `mock-ticketing` HTTP 服务，数据写入 MySQL 表 `aiops_history_tickets`；创建工单也可落 MySQL | 用来解释相似故障复盘，不替代实时证据 |
| Alertmanager 查询、Jaeger/Tempo、Redpanda/Kafka、Grafana/Otel/Attu | 不作为校招主线，相关查询适配器和高级 compose 组件已删除或下线 | 避免把项目讲成大而全平台 |
| MCP mock | `mcp_servers/cls_server.py`、`monitor_server.py` 仍保留为本地 fallback / 协议演示；校招真实适配器验证不依赖它 | 可以讲降级机制，但不要宣传成真实生产接入 |
| 测试 Fake / MockTransport / eval fixture | 只用于单元测试、契约测试和离线评测 | 这是工程测试手段，不是运行时数据来源 |

一句话总结：

> 校招主线里，指标、日志、Redis、MySQL、服务目录、发布历史和历史工单都已经有真实本地适配器或真实 MySQL/Redis 数据承载；保留下来的 mock 主要是测试替身、离线评测 fixture 和 MCP fallback 演示，不会包装成真实生产证据。

## 7. 面试演示路径

10 分钟只讲一条主线：

```text
RAG 可信问答
  -> Incident 诊断输入
  -> Plan-Execute-Replan
  -> 工具调用和 Evidence
  -> 风险审批或 forbidden
  -> Trace / Report / Eval
```

推荐节奏：

| 时间 | 展示内容 | 讲清楚什么 |
| --- | --- | --- |
| 0:00-1:00 | README 和工作台首页 | 项目定位：不是聊天机器人，而是 OnCall 诊断闭环 |
| 1:00-2:30 | RAG Runbook 问答 | 有可信来源才回答；无来源拒答；回答带 citation |
| 2:30-4:30 | Redis maxclients 或 MySQL slow query demo | Incident 输入后由 Planner 拆成可执行步骤 |
| 4:30-6:30 | 工具调用和证据链 | 工具结果归一为 Evidence / ToolCallRecord，结论来自证据 |
| 6:30-8:00 | 报告、Trace、Incident 状态 | 诊断过程可回放、可审计、可复盘 |
| 8:00-9:00 | 审批或 forbidden 场景 | 高风险动作不会自动执行生产写操作 |
| 9:00-10:00 | 测试和离线评测 | eval 是回归保障，不是线上准确率声明 |

### 7.1 推荐 demo case

| Case | 入口 | 预期工具/证据 | 面试讲法 |
| --- | --- | --- | --- |
| Redis maxclients | 前端模板 `Redis maxclients`；接口 `/api/aiops/demo/incidents/redis_maxclients/run` | `query_redis_status`、`query_metrics`、`query_logs`、Runbook、历史工单 | Redis 连接数接近上限导致 timeout，Agent 通过多源证据形成根因 |
| MySQL slow query | 前端模板 `MySQL slow query`；接口 `/api/aiops/demo/incidents/mysql_slow_query/run` | `query_mysql_status`、`query_metrics`、`query_logs`、Runbook | 慢 SQL 和连接池等待导致延迟，报告说明证据来源 |
| Pod CrashLoop | 前端模板 `Pod CrashLoop`；接口 `/api/aiops/demo/incidents/pod_crashloop/run` | `query_k8s_status`、`query_logs`、`query_metrics`、Runbook | Pod 重启、CrashLoopBackOff 或 OOMKilled 转成结构化 Evidence |
| Forbidden SQL | 前端模板 `Forbidden SQL`；接口 `/api/aiops/demo/incidents/forbidden_sql/run` | Risk Controller、forbidden 证据 | 未审核删除 SQL 被禁止，体现 Agent 安全边界 |

推荐主讲 Redis maxclients，因为它最容易串起：

```text
告警现象
  -> Redis 连接数证据
  -> 指标和日志佐证
  -> Runbook 建议
  -> 报告和风险边界
```

### 7.2 演示前命令

基础演示：

```bash
make bootstrap
make up
make dev
make upload
```

校招真实适配器沙箱：

```bash
make interview-up
make sandbox-verify
make sandbox-demo
```

这组命令会启动校招核心适配器栈：MySQL、Redis、metrics-exporter、Prometheus、Loki 和 loki-log-emitter，并把服务目录、发布历史、历史工单、事故证据写入真实 MySQL / Redis 容器。报告和 trace 中应看到 `redis_info`、`mysql`、`prometheus`、`loki`、`cmdb`、`deploy_history`、`ticket_api` 等来源，而不是 `mock`。

默认 5 分钟面试主线建议只启动 interview stack，不让 Milvus/RAG 抢 Redis / MySQL live adapter 叙事；需要展示多源 RAG 时，再单独运行 `make up && make upload`，并用 PDF 复盘、HTML Wiki、CSV/XLSX 历史工单对应 Redis / MySQL RCA。

固定报告资产：

```bash
make demo-reports
```

面试前质量验证：

```bash
make verify
```

时间紧时至少：

```bash
make test-quick
make eval
make eval-rag
make eval-change
```

## 8. 常见追问

### Q1：为什么不用纯 RAG？

纯 RAG 适合回答 Runbook 问题，但不能主动查询当前指标、日志、Redis、MySQL、K8s 状态。AutoOnCall 把 RAG 作为知识依据，把实时诊断交给 Plan-Execute-Replan 和工具证据。

### Q2：怎么避免模型幻觉？

RAG 无可信来源时拒答；Planner 使用结构化输出和 fallback；Executor 的证据来自工具结果；报告生成基于 Evidence、ToolCallRecord 和 RiskAssessment，而不是完全交给模型自由生成。

### Q3：真实系统没配置怎么办？

校招主线默认使用 Docker 中的真实 Redis、MySQL、Prometheus、Loki，以及 MySQL 中的服务目录、发布历史和历史工单 seed 数据。严格环境设置 `AIOPS_MOCK_FALLBACK_ENABLED=false` 后，未配置工具会返回结构化 `not_configured` 或 `failed`，不会伪装成真实证据。项目仍保留 MCP mock/fallback 和离线 eval fixture，但它们只用于降级机制演示、测试和离线评测，不作为主线真实取证来源。

### Q4：审批通过后会自动执行吗？

不会。审批通过后可以恢复诊断闭环，也可以进入安全变更流程，但安全变更只支持 pre-check、dry-run、sandbox 或人工执行记录，不自动修改生产环境。

### Q5：为什么用 SQLite？

SQLite 适合本地演示和单副本运行，能持久化 Trace、审批、报告、告警、会话快照和事件状态。项目也支持 `AIOPS_STORAGE_BACKEND=mysql`，多副本或生产化应切 MySQL。

### Q6：评测通过率可以写成线上准确率吗？

不可以。离线评测只能说明当前确定性 case 的回归结果。简历和面试里应说“建立了离线评测集，结果以最新执行输出为准”，不要写成线上准确率。

### Q7：项目还差什么生产化能力？

主要是 SSO/OIDC、后台任务队列、数据库 migration、多副本部署治理、组织级审批策略、审计防篡改、线上反馈闭环和更大规模真实样本治理。这些是生产化边界，不影响校招主项目成立。

## 9. 两个月冲刺计划

两个月内不要追求大而全。目标是让面试官在 10 分钟内相信：

> AutoOnCall 不是普通 RAG，而是一个有业务场景、有工具取证、有安全边界、有评测指标、有报告闭环的 AIOps Agent。

### 第 1-2 周：跑稳演示闭环

交付物：

- Redis / MySQL 两个真实适配器 demo case，以及 K8s / forbidden action 的边界说明。
- 每个 case 的预期工具、预期证据、预期报告结论。
- `make demo-reports` 生成的 3 份 Markdown 报告。
- README 和本文件中的演示口径一致。

验收：

- 至少 1 条 Redis 或 MySQL demo 能现场稳定跑通。
- 报告样例能打开，并能说明实时事实、历史上下文和推断之间的区别。
- 报告明确数据源是 `redis_info`、`mysql`、`prometheus`、`loki`、`cmdb`、`deploy_history`、`ticket_api`、`not_configured` 还是 `failed`，不把 mock 包装成真实证据。

### 第 3-4 周：强化证据驱动 RCA

交付物：

- supporting / refuting / unknown evidence 更清楚地进入报告。
- 增加 contradiction case，例如“日志怀疑 Redis，但 Redis 状态正常”。
- 工具失败、权限不足、超时都能生成 unknown evidence。
- Redis / MySQL 报告中增加证据矩阵：实时证据、知识依据、历史经验分层展示。
- 根因、关键发现、处置建议三类关键结论关联 evidence_id 或 citation。

验收：

- Redis 正常但日志 timeout 时，不强判 Redis maxclients。
- MySQL 正常但 K8s 有 OOMKilled 时，根因排序能体现 K8s 更可疑。
- 工具失败时报告可生成，但置信度和限制说明要准确。
- Redis / MySQL golden report 中至少能指出 1 条实时证据、1 条知识依据和 1 条历史经验。
- 关键结论无法关联证据时，报告状态或置信度要降级，而不是强行输出 completed RCA。

### 第 5-6 周：多源 RAG 与评测 summary

交付物：

- AIOps case 从当前 16 个提升到 20-25 个高质量 case。
- contradiction case 至少 5 个。
- tool failure case 至少 3 个。
- forbidden action case 覆盖危险 SQL、删 Pod、危险 shell。
- summary 里展示 RCA 命中、工具命中、反证识别、unknown evidence、风险策略、报告完整率。
- RAG summary 展示 Markdown / PDF / HTML / table 的 indexed_units、dropped_units、warning_file_count 和 citation coverage。
- Redis / MySQL golden chain 中至少各有一个 case 使用 PDF 复盘、HTML Wiki 或表格历史工单作为知识/历史依据。

验收：

- `make eval` 输出 JSON / Markdown summary。
- `make eval-rag`、`make eval-change` 能作为独立门禁说明。
- 文档不写固定通过率，统一说“以当前执行结果为准”。
- 多源 RAG 不是孤立 demo：评测或报告中能说明它如何支撑故障 RCA。

### 第 7-8 周：收敛面试材料

交付物：

- 简历表述、10 分钟讲解稿、常见追问答案。
- 3 份报告样例。
- 一组截图或工作台页面路径。
- mock / sandbox / real adapter 边界话术。

验收：

- 项目 30 秒、3 分钟、10 分钟三个版本都能讲清。
- 不需要现场翻代码找入口。
- 不把 demo、mock、离线 eval 包装成生产系统。

## 10. P1 / P2 后续路线

### P1：显著拉开工程深度

| 方向 | 建议 |
| --- | --- |
| 工具输出预算和 artifact | 大日志不直接进 prompt，记录 `raw_artifact_path`、`output_size_bytes`、`truncated`、`truncation_reason` |
| 证据矩阵产品化 | 报告固定展示“实时证据 / 知识依据 / 历史经验 / 缺失证据”，并把每条根因假设回链到 evidence_id |
| 多源 RAG 质量门禁 | 将 `IndexingQualityService` 聚合指标写入 eval summary，用 dropped/warning/citation coverage 判断知识入库质量 |
| 变更与历史经验参与假设排序 | 服务拓扑、最近变更、历史工单参与假设排序，但不能单独决定根因 |
| K8s / Trace 场景 | K8s 可后续接真实 API 或更轻量的本地 fixture；Trace 不作为当前校招主线，等 Redis/MySQL/日志/指标链路足够稳后再补 |

### P2：锦上添花

| 方向 | 建议 |
| --- | --- |
| 后台任务队列 | 长诊断从单 SSE 请求解耦为任务状态机 |
| 更正式 RBAC / SSO | 当前 token RBAC 可支撑演示，企业化再接 OIDC |
| 数据库 migration | 当前 SQLite/MySQL 可用，企业化再引入 Alembic 等迁移体系 |
| 工作台体验 | 重点展示 Evidence 矩阵、Trace 时间线、报告预览和审批状态，不重写炫技前端 |

## 11. 不建议优先做

- 不做多 Agent 群聊式重构。
- 不做自动生产修复。
- 不做复杂前端重写。
- 不为了数量堆随机工具。
- 不做模型微调，除非已有稳定数据集和明确收益。
- 不把 mock/fallback 包装成真实生产接入。
- 不在简历里写“线上准确率 100%”。

## 12. 推荐简历表述

稳妥版本：

```text
基于 FastAPI、LangGraph、Milvus 和 DashScope 构建面向 OnCall 故障诊断场景的 RAG + AIOps Agent 系统，支持 Alertmanager 告警接入、Runbook 检索、结构化 Incident 诊断、工具取证、人工审批、Trace 追踪、诊断报告和安全变更 dry-run 闭环。

设计多源企业知识入库链路，支持 Markdown、PDF、HTML、CSV、XLSX，保留 page、heading_path、sheet、row、primary_key 等可审计引用元数据，并记录 loader 清洗质量报告；通过 hybrid search、rerank、trust gate 和 citation guard 实现有来源回答与无可信来源拒答。

设计 Plan-Execute-Replan 诊断流程，将故障排查拆分为 Planner、Executor、Evidence Analyzer、Replanner 和 Report Generator；通过 Tool Registry 统一指标、日志、Redis、MySQL、Kubernetes、Runbook、服务目录、发布历史、历史工单等工具调用，并将工具结果归一为 Evidence 和 ToolCallRecord。

设计 Human-in-the-loop 风险控制链路，对只读排查自动放行，对重启、扩缩容、回滚、配置变更等中高风险动作生成审批请求，对删除 Pod、未审核 SQL、危险 shell 等动作直接阻断，避免 Agent 自动执行生产变更。

构建离线评测集覆盖 AIOps、RAG、安全变更和 Replanner 场景，验证工具命中、根因命中、审批触发、禁止动作拦截、工具失败降级、RAG 引用和无答案拒答；评测结果以 JSON/Markdown 报告形式可复现。
```

升级版本要等对应能力确实落地后再写：

```text
参考 SRE Agent 工程实践，在本地 interview Docker stack 中构造 Redis maxclients、MySQL slow query 等可复现故障场景；通过真实 Redis/MySQL/Prometheus/Loki 适配器和 MySQL 中的服务目录、发布历史、历史工单验证 RCA 命中率、工具命中率、证据来源可信度和报告完整率，并用 PDF 复盘、HTML Wiki、CSV/XLSX 历史表格补充知识与历史经验依据。
```

## 13. 最后判断

AutoOnCall 当前最好的路线不是继续扩成企业平台，而是：

> 少加功能，多增强可信度；少讲生产级，多讲边界和验证。

面试官最需要相信五件事：

1. 你知道大模型应用不只是调 API。
2. 你理解 RAG 的可信来源、引用和拒答。
3. 你知道 Agent 必须有工具边界和风险控制。
4. 你能用测试和评测证明系统行为。
5. 你不会把 mock demo 夸成生产系统。

做到这些，这个项目已经足够成为一份很强的校招生大模型开发作品。
