# AutoOnCall

AutoOnCall 是一个面向 OnCall 故障诊断场景的 Python 3.11 FastAPI 应用。项目当前定位是“大模型应用工程 / AIOps Agent 原型”，核心不是普通聊天，而是把告警接入、RAG Runbook、Plan-Execute-Replan 诊断、工具取证、风险审批、Trace、报告和安全变更记录串成一条可解释闭环。

当前版本：`1.2.1`。

## 当前能力

- RAG 知识库问答：支持 Markdown / 文本上传、结构化切分、DashScope Embedding、Milvus 向量检索、本地词法索引、hybrid search、rerank、引用补齐和无可信来源拒答。
- 告警自动接入：支持 Alertmanager webhook，标准化 `alerts[]`，按 fingerprint 去重，创建或更新 `inc-alert-*` Incident，并保留告警恢复状态。
- AIOps 诊断 Agent：基于 LangGraph 的 `planner -> executor -> replanner` 流程，结合指标、日志、Trace、K8s、Redis、MySQL、消息队列、CMDB、发布历史、工单和 Runbook 等工具取证。
- 风险控制与人工审批：只读诊断自动执行，中高风险动作进入审批，危险动作直接禁止。
- 安全变更闭环：审批后可进入 pre-check、dry-run、sandbox 或人工执行记录，系统不会自动执行生产写操作。
- 可观测和复盘：Trace、ToolCall、Evidence、Approval、DiagnosisReport、IncidentState、ChangeExecution、SessionSnapshot 和 AlertEvent 可写入 SQLite 或 MySQL。
- 前端工作台：FastAPI 直接挂载 `static/`，支持 RAG 问答、故障诊断、告警事件、Incident 详情、Trace、报告、审批、变更和评测面板。
- 离线评测：`eval/cases.yaml`、`eval/rag_cases.yaml`、`eval/change_cases.yaml` 覆盖 AIOps、RAG 和安全变更核心行为。

## 核心链路

### RAG 问答

```text
POST /api/chat 或 /api/chat_stream
  -> RagAgentService
  -> retrieve_structured_knowledge
  -> Milvus + LexicalIndex hybrid search
  -> L2 阈值过滤可信来源
  -> ChatQwen grounded answer
  -> citations / source_file / chunk_id 引用兜底
```

关键边界：

- 无可信知识来源时拒答，不让模型自由发挥。
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
- 默认不保存完整外部原始 payload，只保留精简内容；确需排障时可显式开启 `AIOPS_STORE_RAW_EXTERNAL_PAYLOAD=true`。
- `auto_diagnose=true` 可后台触发现有 AIOps 诊断流，默认关闭，避免阻塞 Alertmanager 回调。

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
  -> SafeChangeWorkflow
  -> pre-check -> dry-run -> dry_run_only / sandbox / manual_record
  -> ChangeExecution + Trace + Report snapshot
```

关键边界：

- `dry_run_only` 只校验计划、回滚方案和观察指标。
- `manual_record` 等待人工提交执行结果后生成观察和回滚建议。
- `sandbox` 只用于本地沙箱或明确开启的非生产执行路径。

## 目录结构

```text
app/
  api/              FastAPI 路由：chat、file、aiops、alerts、approvals、incidents、health、eval
  agent/aiops/      LangGraph AIOps Agent 节点、状态、证据分析和风险控制
  core/             鉴权、Milvus 等基础设施封装
  integrations/     Alertmanager、Prometheus、Loki/日志网关、K8s、Redis、MySQL、CMDB、Ticket 等适配器
  models/           Pydantic 请求、告警、事件、证据、审批、报告和变更模型
  services/         RAG、索引、AIOps 编排、存储、Trace、审批、报告和告警接入服务
  tools/            Agent 工具抽象、工具注册表和本地工具
aiops-docs/         写入向量库的运维 Runbook Markdown
config/             服务拓扑等本地配置
deploy/             生产配置说明和本地 full-stack sandbox
eval/               AIOps、RAG、安全变更离线评测用例
mcp_servers/        可选本地 MCP mock 服务
scripts/            评测、迁移、清理、沙箱验证脚本
static/             前端工作台
tests/              pytest 测试
文档/                项目分析书和面试讲解文档
```

不应提交的本地产物包括：`venv/`、`.env`、`logs/`、`data/*.db*`、`uploads/`、`htmlcov/`、`.coverage`、`__pycache__/`、`.pytest_cache/`、`.ruff_cache/`、`.idea/`、Milvus 或 Docker 数据卷。

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
make dev
make upload
```

启动完整本地 AIOps 沙箱：

```bash
make sandbox-up
powershell -ExecutionPolicy Bypass -File deploy\full-stack\seed-demo-data.ps1
make sandbox-verify
make sandbox-demo
```

沙箱会启动 Redis、MySQL、Prometheus、Alertmanager、Grafana、Loki、Kubernetes API mock、Tempo、Jaeger、OpenTelemetry Collector、Redpanda，以及 CMDB、工单和发布历史 mock 服务。详细说明见 `deploy/sandbox.md`。

Windows 下也可以使用：

```powershell
.\start-windows.bat
.\stop-windows.bat
```

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
- RAG：`RAG_TOP_K`、`RAG_MAX_L2_DISTANCE`、`RAG_HYBRID_SEARCH_ENABLED`、`RAG_RERANK_ENABLED`、`INDEX_ALLOWED_ROOTS`
- AIOps 状态：`AIOPS_STORAGE_BACKEND`、`AIOPS_SQLITE_PATH`、`MYSQL_DSN`
- Mock 边界：`AIOPS_MOCK_FALLBACK_ENABLED`
- 原始外部 payload：`AIOPS_STORE_RAW_EXTERNAL_PAYLOAD`
- CORS：`CORS_ALLOWED_ORIGINS`
- API 鉴权：`API_AUTH_ENABLED`、`API_READ_TOKEN`、`API_OPERATOR_TOKEN`、`API_APPROVER_TOKEN`、`API_ADMIN_TOKEN`、`API_AUTH_TOKENS`
- 外部适配器：`ALERTMANAGER_BASE_URL`、`PROMETHEUS_BASE_URL`、`LOG_GATEWAY_URL`、`LOKI_BASE_URL`、`JAEGER_BASE_URL`、`TEMPO_BASE_URL`、`KUBERNETES_API_SERVER`、`REDIS_URL`、`MYSQL_DSN`、`CMDB_API_URL`、`DEPLOY_HISTORY_API_URL`、`TICKET_API_URL`

默认 `API_AUTH_ENABLED=false`，适合本地 demo 和测试。内网或生产化环境应开启 token RBAC；更正式的生产环境应接入 SSO/OIDC，并在网关或应用层统一治理身份。

## 质量验证

常用验证：

```bash
make lint
make test-quick
make eval
make eval-rag
make eval-change
```

面试或交付前的本地门禁：

```bash
make verify-local
```

如果浏览器 smoke 测试在受限环境下因为无法绑定本地端口失败，可以先跑非浏览器测试，再在本机可开放端口的环境补测前端。

## 安全边界

- 这是 AIOps Agent 原型和工程化验证项目，不应声称已经接入真实生产系统。
- 本地演示可能使用 mock/fallback；生产或严格验收应设置 `AIOPS_MOCK_FALLBACK_ENABLED=false`。
- 外部响应和告警 webhook 默认保持精简存储，生产建议保留 `AIOPS_STORE_RAW_EXTERNAL_PAYLOAD=false`。
- API token RBAC 已能支撑内网演示和轻量准入，生产仍建议接入 SSO/OIDC。
- 安全变更链路只支持 dry-run、sandbox 或人工执行记录，不自动执行重启、删 Pod、执行 SQL 或修改生产配置。
- SQLite 适合单机本地或单副本演示，多副本部署应切 MySQL 并配合备份和迁移策略。
- 离线评测用于稳定回归，不代表线上真实准确率。
