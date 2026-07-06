# AutoOnCall

AutoOnCall 是一个面向 OnCall 故障诊断场景的 Python 3.11 FastAPI 应用。它把告警接入、RAG Runbook、Plan-Execute-Replan 诊断、工具取证、风险审批、Trace、报告和安全变更记录串成一条可解释、可审计、可评测的 AIOps 闭环。

当前版本：`1.2.1`。

## 项目叙事

线上故障排查通常不是“问模型一个问题，然后等答案”这么简单。OnCall 工程师需要同时查看告警、指标、日志、Trace、K8s 状态、Redis/MySQL 状态、发布记录、历史工单和 Runbook；如果排查过程只停留在聊天记录里，事后很难复盘模型为什么给出某个结论；如果让 Agent 直接执行重启、删 Pod、执行 SQL 或修改配置，又会把自动化工具变成新的生产风险。

AutoOnCall 的设计目标是把大模型放进一个受控的运维工程闭环里：

```text
Alert / Incident
  -> Planner 拆解排查计划
  -> Executor 通过 Tool Registry 调用指标、日志、Trace、Redis、MySQL、K8s、Runbook 等工具
  -> Evidence + ToolCallRecord 沉淀证据链
  -> Replanner 判断补查、审批、报告或升级人工
  -> Trace + Report + IncidentState 支撑复盘
  -> 高风险动作只进入审批、dry-run、sandbox 或人工执行记录
```

这个项目的重点不是包装“模型能回答”，而是证明大模型应用可以被放进清晰的后端边界里：RAG 只提供有引用的知识依据，工具只通过统一契约执行，证据和 Trace 可回放，风险动作不会自动改生产，测试和离线评测用于防止核心链路回归。

适合在简历中概括为：

> 面向 OnCall 场景的 RAG + AIOps Agent 故障诊断系统，基于 FastAPI、LangGraph、Milvus 和 Pydantic 实现告警接入、Runbook 检索、Plan-Execute-Replan 诊断、工具取证、证据链追踪、人工审批、诊断报告和安全变更 dry-run 闭环。

## 面试演示路径（10 分钟）

面试演示时不要平均展示所有页面，建议只讲一条主线：

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
| 9:00-10:00 | 测试和离线评测 | 说明质量门禁和 eval 是回归保障，不是线上准确率声明 |

详细讲解脚本见 [从零上手与面试讲解](文档/AutoOnCall项目从零上手与面试讲解.md)。

## 项目边界

当前阶段目标是围绕校招展示继续打磨稳定闭环：优先让 Redis / MySQL / K8s demo、证据链、报告样例和评测指标更可信，而不是扩成大而全的企业平台。

持续保留的边界：

- 不把本地 demo、mock/fallback 或离线评测结果包装成真实生产能力。
- 不引入自动生产写操作；审批通过后仍只进入 dry-run、sandbox 或人工执行记录。
- 新增能力优先服务于端到端演示、证据驱动 RCA、评测指标、报告样例和安全边界。
- 不为了炫技重写成复杂多 Agent 群聊、复杂 React 前端或平台化系统。

两个月校招冲刺建议见 [校招竞争力改进计划](文档/AutoOnCall校招竞争力改进计划.md)。

## 核心能力

- RAG 知识库问答：支持 Markdown / 文本上传、结构化切分、DashScope Embedding、Milvus 向量检索、本地词法索引、hybrid search、rerank、引用补齐和无可信来源拒答。
- 告警自动接入：支持 Alertmanager webhook，按 fingerprint 去重，创建或更新 Incident，并保留 firing / resolved 状态。
- AIOps 诊断 Agent：基于 LangGraph 的 `planner -> executor -> replanner` 流程，结合指标、日志、Trace、K8s、Redis、MySQL、消息队列、CMDB、发布历史、工单和 Runbook 等工具取证。
- 风险控制与人工审批：只读诊断自动执行，中高风险动作进入审批，危险动作直接禁止。
- 安全变更闭环：审批后可进入 pre-check、dry-run、sandbox 或人工执行记录，系统不会自动执行生产写操作。
- 可观测和复盘：Trace、ToolCall、Evidence、Approval、DiagnosisReport、IncidentState、ChangeExecution、SessionSnapshot 和 AlertEvent 可写入 SQLite 或 MySQL。
- 前端工作台：FastAPI 直接挂载 `static/`，支持 RAG 问答、故障诊断、告警事件、Incident 详情、Trace、报告、审批、变更和评测面板。
- 离线评测：`eval/cases.yaml`、`eval/rag_cases.yaml`、`eval/change_cases.yaml`、`eval/replanner_cases.yaml` 覆盖 AIOps、RAG、安全变更和 Replanner LLM 决策核心行为。

## 核心链路

### RAG 问答

```text
文档入库
  -> POST /api/upload 或 make upload
  -> 文件名 / 扩展名 / 大小校验
  -> Markdown 结构化切分
  -> DashScope Embedding
  -> Milvus 向量索引
  -> 本地 LexicalIndex 词法索引

用户提问
  -> POST /api/chat 或 /api/chat_stream
  -> retrieve_structured_knowledge
  -> 向量召回 + 词法召回
  -> 候选合并去重
  -> rerank: 向量距离 + 词法重合 + 原始排序
  -> trust gate: L2 距离阈值 / 词法可信阈值
  -> 有可信片段: grounded answer
  -> 无可信片段: refuse_without_trusted_source
  -> citation guard: 回答必须带 source_file + chunk_id
```

关键边界：

- 每个成功回答都必须带 citation；缺少 `source_file + chunk_id` 时会拒答。
- 无可信知识来源时稳定拒答，不让模型自由发挥。
- `top_k` 只代表候选召回，不等同于可信；回答前还要经过距离阈值、词法阈值和引用校验。
- 目录索引只允许 `INDEX_ALLOWED_ROOTS` 中的安全目录。
- RAG 结果用于知识依据，不等同于实时线上事实。

### 告警接入

```text
POST /api/alerts/alertmanager
  -> AlertIngestionService
  -> AlertEvent 标准化
  -> fingerprint 去重
  -> IncidentState 创建或更新
  -> GET /api/alerts 与 /api/incidents 可见
```

关键边界：

- 优先使用 Alertmanager 自带 fingerprint；缺失时根据 `alertname + service + environment + key labels` 生成稳定指纹。
- 重复 firing webhook 不会重复建 Incident。
- resolved webhook 会更新告警状态；如果 Incident 已进入审批、变更等更深生命周期，不会被恢复告警覆盖。
- 默认不保存完整外部原始 payload；确需排障时可显式开启 `AIOPS_STORE_RAW_EXTERNAL_PAYLOAD=true`。

### AIOps 诊断

```text
POST /api/aiops
  -> SSE 事件流
  -> AIOpsService
  -> LangGraph: planner -> executor -> replanner
  -> ToolRegistry / integrations / RAG runbook
  -> Evidence / ToolCallRecord / RiskAssessment
  -> Trace / Approval / Report / IncidentState
```

关键边界：

- Planner 生成结构化 `PlanStep`，LLM 失败时使用规则 fallback。
- Executor 只通过 Tool Registry 执行工具，并把结果归一成 Evidence。
- Replanner 根据证据充分性、工具失败、风险动作和最大步数决定补查、审批、报告或升级人工。
- mock/fallback 会在数据源字段中显式体现；严格环境应设置 `AIOPS_MOCK_FALLBACK_ENABLED=false`。

### 安全变更

```text
Approval approved
  -> POST /api/incidents/{incident_id}/changes/{change_plan_id}/resume
  -> pre-check
  -> dry-run
  -> dry_run_only / sandbox / manual_record
  -> ChangeExecution + Trace + Report snapshot
```

关键边界：

- `dry_run_only` 只校验计划、回滚方案和观察指标。
- `manual_record` 等待人工提交执行结果后生成观察和回滚建议。
- `sandbox` 只用于本地沙箱或明确开启的非生产执行路径。

## 快速开始

安装依赖：

```bash
python3.11 -m venv venv
. venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

也可以使用：

```bash
make bootstrap
```

启动 FastAPI：

```bash
make dev
```

打开：

- 前端工作台：http://localhost:9900
- OpenAPI：http://localhost:9900/docs
- Liveness：http://localhost:9900/health/live
- Readiness：http://localhost:9900/health/ready

启动 Milvus 并上传 Runbook：

```bash
make up
make upload
```

启动完整本地 AIOps 沙箱：

```bash
make sandbox-up
powershell -ExecutionPolicy Bypass -File deploy\full-stack\seed-demo-data.ps1
make sandbox-verify
make sandbox-demo
```

沙箱会启动 Redis、MySQL、Prometheus、Alertmanager、Grafana、Loki、Kubernetes API mock、Tempo、Jaeger、OpenTelemetry Collector、Redpanda，以及 CMDB、工单和发布历史 mock 服务。详细说明见 [沙箱说明](deploy/sandbox.md)。

Windows 下也可以使用：

```powershell
.\scripts\dev\start-windows.bat
.\scripts\dev\stop-windows.bat
```

可选容器镜像构建：

```bash
docker build -t autooncall:local .
docker run --rm -p 9900:9900 --env-file .env autooncall:local
```

容器入口只负责启动 FastAPI 和静态工作台；Milvus、MySQL、Redis、Prometheus、Loki、K8s mock 等依赖仍应通过 compose、托管服务或内网平台显式配置。`.dockerignore` 会排除 `.env`、虚拟环境、日志、上传文件、SQLite 数据库和覆盖率报告，避免把本地产物打进镜像。

面试演示脚本和讲解顺序见 [从零上手与面试讲解](文档/AutoOnCall项目从零上手与面试讲解.md)。

## 常用接口

| 功能 | 方法 | 路径 |
| --- | --- | --- |
| RAG 对话 | POST | `/api/chat` |
| RAG 流式对话 | POST | `/api/chat_stream` |
| 文件上传并索引 | POST | `/api/upload` |
| 批量目录索引 | POST | `/api/index_directory` |
| Alertmanager 告警接入 | POST | `/api/alerts/alertmanager` |
| 告警列表 | GET | `/api/alerts` |
| 告警详情 | GET | `/api/alerts/{fingerprint}` |
| AIOps 诊断 | POST | `/api/aiops` |
| Demo Incident | GET | `/api/aiops/demo/incidents` |
| AIOps 运行历史 | GET | `/api/aiops/runs` |
| 工具契约 | GET | `/api/aiops/tools/contracts` |
| Incident 列表 | GET | `/api/incidents` |
| Incident 详情 | GET | `/api/incidents/{incident_id}` |
| Trace | GET | `/api/incidents/{incident_id}/trace` |
| 报告 | GET | `/api/incidents/{incident_id}/report` |
| 待审批列表 | GET | `/api/approvals/pending` |
| 提交审批 | POST | `/api/incidents/{incident_id}/approval` |
| 审批后恢复诊断闭环 | POST | `/api/incidents/{incident_id}/diagnosis/resume` |
| 启动安全变更 | POST | `/api/incidents/{incident_id}/changes/{change_plan_id}/resume` |
| 变更列表 | GET | `/api/incidents/{incident_id}/changes` |
| 变更详情 | GET | `/api/changes/{change_execution_id}` |
| 人工执行记录 | POST | `/api/changes/{change_execution_id}/manual-result` |
| 评测摘要 | GET | `/api/eval/summary` |
| 适配器验收摘要 | GET | `/api/eval/adapter-verification` |
| 进程探活 | GET | `/health/live` |
| 依赖就绪 | GET | `/health/ready` |

## 配置

配置默认值在 `app/config.py`，生产或本地覆盖可参考 `.env.example` 和 `deploy/sandbox.env`。

常用配置：

- DashScope：`DASHSCOPE_API_KEY`、`DASHSCOPE_API_BASE`、`DASHSCOPE_MODEL`、`DASHSCOPE_EMBEDDING_MODEL`、`RAG_MODEL`
- Milvus：`MILVUS_HOST`、`MILVUS_PORT`、`MILVUS_RECREATE_ON_DIMENSION_MISMATCH`
- RAG：`RAG_TOP_K`、`RAG_MAX_L2_DISTANCE`、`RAG_MIN_LEXICAL_TRUST_SCORE`、`RAG_HYBRID_SEARCH_ENABLED`、`RAG_RERANK_ENABLED`、`INDEX_ALLOWED_ROOTS`
- AIOps 状态：`AIOPS_STORAGE_BACKEND`、`AIOPS_SQLITE_PATH`、`MYSQL_DSN`、`AIOPS_REPLANNER_LLM_ENABLED`
- A2A 北向协作：`A2A_ENABLED`、`A2A_BASE_PATH`、`A2A_AGENT_NAME`
- Mock 边界：`AIOPS_MOCK_FALLBACK_ENABLED`
- 原始外部 payload：`AIOPS_STORE_RAW_EXTERNAL_PAYLOAD`
- CORS：`CORS_ALLOWED_ORIGINS`
- API 鉴权：`API_AUTH_ENABLED`、`API_READ_TOKEN`、`API_OPERATOR_TOKEN`、`API_APPROVER_TOKEN`、`API_ADMIN_TOKEN`、`API_AUTH_TOKENS`
- 外部适配器：`ALERTMANAGER_BASE_URL`、`PROMETHEUS_BASE_URL`、`LOG_GATEWAY_URL`、`LOKI_BASE_URL`、`JAEGER_BASE_URL`、`TEMPO_BASE_URL`、`KUBERNETES_API_SERVER`、`REDIS_URL`、`MYSQL_DSN`、`CMDB_API_URL`、`DEPLOY_HISTORY_API_URL`、`TICKET_API_URL`

默认 `API_AUTH_ENABLED=false`，适合本地 demo 和测试；默认 `AIOPS_MOCK_FALLBACK_ENABLED=false`，避免生产环境漏配外部系统时生成合成诊断证据；默认 `AIOPS_REPLANNER_LLM_ENABLED=false`，Replanner 先使用确定性证据分析，启用后才调用结构化 LLM 决策；默认 `A2A_ENABLED=false`，A2A 只作为受信任 Agent 运行时调用 AutoOnCall 诊断、状态、Replay 和 Runbook 问答的北向协作入口，不暴露底层工具、审批决策或生产变更执行。本地 demo 如需离线演示，可显式打开 mock fallback。内网或生产化环境应开启 token RBAC；更正式的生产环境应接入 SSO/OIDC，并在网关或应用层统一治理身份。

## 质量验证

交付前本地门禁：

```bash
make verify
```

`make verify` 是只验证入口，不会格式化或修改源码；历史兼容入口 `make check-all` 等同于 `make verify`。需要自动修复格式或导入顺序时，再单独运行：

```bash
make fix
```

门禁拆开执行：

```bash
make format-check
make lint
make type-check
make security
make test-quick
make eval
make eval-rag
make eval-change
make eval-replanner
make hygiene-check
```

`make security` 运行 Bandit 中高风险扫描。`make hygiene-check` 只报告不删除，默认关注可能误提交的未忽略生成物；需要审计本地缓存、虚拟环境、日志、数据库和上传产物时可直接运行：

```bash
python scripts/maintenance/hygiene_check.py --include-ignored
```

如果浏览器 smoke 测试在受限环境下因为无法绑定本地端口失败，可以先跑非浏览器测试，再在本机可开放端口的环境补测前端。

CI 入口位于 `.github/workflows/quality.yml`，同样执行 `make verify`，确保本地交付门禁和 CI 门禁一致。

## 目录结构

```text
app/
  api/              FastAPI 路由：chat、file、aiops、alerts、approvals、incidents、health、eval
  agent/aiops/      LangGraph AIOps Agent 节点、状态、证据分析和风险控制
  core/             鉴权、Milvus 等基础设施封装
  integrations/     Alertmanager、Prometheus、Loki/日志网关、K8s、Redis、MySQL、CMDB、Ticket 等适配器
  models/           Pydantic 请求、告警、事件、证据、审批、报告和变更模型
  services/         RAG、索引、AIOps 编排、存储、Trace、审批、报告、告警接入和读模型服务
  tools/            Agent 工具抽象、工具注册表和本地工具
aiops-docs/         写入向量库的运维 Runbook Markdown
config/             服务拓扑等本地配置
deploy/             生产配置说明和本地 full-stack sandbox
eval/               AIOps、RAG、安全变更、Replanner 离线评测用例
mcp_servers/        可选本地 MCP mock 服务
scripts/            评测、迁移、清理、沙箱验证脚本
static/             前端工作台，static/js/ 存放拆分后的业务脚本
tests/              pytest 测试
文档/                项目分析、校招计划和面试讲解文档
```

不应提交的本地产物包括：`venv/`、`.env`、`logs/`、`data/*.db*`、`uploads/`、`htmlcov/`、`.coverage`、`__pycache__/`、`.pytest_cache/`、`.ruff_cache/`、`.idea/`、Milvus 或 Docker 数据卷。

## 安全边界

- 这是 AIOps Agent 原型和工程化验证项目，不应声称已经接入真实生产系统。
- 本地演示可能使用 mock/fallback；生产或严格验收应设置 `AIOPS_MOCK_FALLBACK_ENABLED=false`。
- 外部响应和告警 webhook 默认保持精简存储，生产建议保留 `AIOPS_STORE_RAW_EXTERNAL_PAYLOAD=false`。
- API token RBAC 已能支撑内网演示和轻量准入，生产仍建议接入 SSO/OIDC。
- 服务绑定到 `0.0.0.0` 且鉴权关闭或 CORS 全开放时，启动日志会给出生产暴露配置提示。
- 安全变更链路只支持 dry-run、sandbox 或人工执行记录，不自动执行重启、删 Pod、执行 SQL 或修改生产配置。
- SQLite 适合单机本地或单副本演示，多副本部署应切 MySQL 并配合备份和迁移策略。
- 离线评测用于稳定回归，不代表线上真实准确率。

## 文档导航

- [文档入口](文档/README.md)：推荐阅读顺序、必读三件套和技术长文索引。
- [从零上手与面试讲解](文档/AutoOnCall项目从零上手与面试讲解.md)：项目叙事总纲、演示脚本、代码地图、面试讲法和追问准备。
- [校招竞争力改进计划](文档/AutoOnCall校招竞争力改进计划.md)：两个月冲刺取舍、P0/P1/P2 路线和验收标准。
- [项目分析书](文档/AutoOnCall项目分析书.md)：系统设计、链路拆解和项目价值分析。
- [生产配置说明](deploy/production.md)：生产化边界、配置建议和部署注意事项。
- [本地沙箱说明](deploy/sandbox.md)：full-stack sandbox 组件、启动和验证流程。
